import os
import gc
import time
import random
import warnings

import hydra
from tqdm import tqdm
import numpy as np
import torch
import torch.optim as torch_optim
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from data_loaders import (load_metadata, TotalTextDatasetIter)
from losses import DBLoss
from lr_schedulers import WarmupPolyLR
from models import DBTextModel
from text_metrics import (cal_text_score, running_score)
from utils import (setup_determinism, setup_logger, to_device, visualize_tfb)

warnings.filterwarnings('ignore')


def get_data_loaders(cfg):

    # train
    tt_train_img_fps, tt_train_gt_fps = load_metadata(
        cfg.data.totaltext.train_dir, cfg.data.totaltext.train_gt_dir)
    # test
    tt_test_img_fps, tt_test_gt_fps = load_metadata(
        cfg.data.totaltext.test_dir, cfg.data.totaltext.test_gt_dir)

    totaltext_train_iter = TotalTextDatasetIter(tt_train_img_fps,
                                                tt_train_gt_fps,
                                                is_training=True,
                                                debug=False)
    totaltext_test_iter = TotalTextDatasetIter(tt_test_img_fps,
                                               tt_test_gt_fps,
                                               is_training=False,
                                               debug=False)

    totaltext_train_loader = DataLoader(dataset=totaltext_train_iter,
                                        batch_size=cfg.hps.batch_size,
                                        shuffle=True,
                                        num_workers=1)
    totaltext_test_loader = DataLoader(dataset=totaltext_test_iter,
                                       batch_size=cfg.hps.batch_size,
                                       shuffle=False,
                                       num_workers=1)
    return totaltext_train_loader, totaltext_test_loader


def main(cfg):

    # set determinism
    setup_determinism(42)

    # setup logger
    logger = setup_logger(
        os.path.join(cfg.meta.root_dir, cfg.logging.logger_file))

    # setup log folder
    log_dir_path = os.path.join(cfg.meta.root_dir, "logs")
    if not os.path.exists(log_dir_path):
        os.makedirs(log_dir_path)
    tfb_log_dir = os.path.join(log_dir_path, str(int(time.time())))
    logger.info(tfb_log_dir)
    if not os.path.exists(tfb_log_dir):
        os.makedirs(tfb_log_dir)
    tfb_writer = SummaryWriter(tfb_log_dir)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(device)
    dbnet = DBTextModel().to(device)

    # load best cp
    if cfg.model.finetune_cp_path:
        cp_path = os.path.join(cfg.meta.root_dir, cfg.model.finetune_cp_path)
        if os.path.exists(cp_path):
            logger.info("Loading best checkpoint: {}".format(cp_path))
            dbnet.load_state_dict(torch.load(cp_path, map_location=device))

    dbnet.train()
    criterion = DBLoss(alpha=cfg.optimizer.alpha,
                       beta=cfg.optimizer.beta,
                       negative_ratio=cfg.optimizer.negative_ratio,
                       reduction=cfg.optimizer.reduction).to(device)
    db_optimizer = torch_optim.Adam(dbnet.parameters(),
                                    lr=cfg.optimizer.lr,
                                    weight_decay=cfg.optimizer.weight_decay,
                                    amsgrad=cfg.optimizer.amsgrad)

    # setup model checkpoint
    best_test_loss = np.inf
    best_train_loss = np.inf

    db_scheduler = None
    lrs_mode = cfg.lrs.mode
    logger.info("Learning rate scheduler: {}".format(lrs_mode))
    if lrs_mode == 'poly':
        db_scheduler = WarmupPolyLR(db_optimizer,
                                    warmup_iters=cfg.lrs.warmup_iters)
    elif lrs_mode == 'reduce':
        db_scheduler = torch_optim.lr_scheduler.ReduceLROnPlateau(
            optimizer=db_optimizer,
            mode='min',
            factor=0.1,
            patience=4,
            verbose=True)

    # get data loaders
    totaltext_train_loader, totaltext_test_loader = get_data_loaders(cfg)

    # train model
    logger.info("Start training!")
    torch.cuda.empty_cache()
    gc.collect()
    global_steps = 0
    for epoch in range(cfg.hps.no_epochs):

        # TRAINING
        dbnet.train()
        train_loss = 0
        running_metric_text = running_score(cfg.hps.no_classes)
        for batch_index, batch in enumerate(totaltext_train_loader):
            lr = db_optimizer.param_groups[0]['lr']
            global_steps += 1

            # resized_image, prob_map, supervision_mask, threshold_map, text_area_map  # noqa
            batch = to_device(batch, device=device)
            img_fps, imgs, prob_maps, supervision_masks, threshold_maps, text_area_maps = batch  # noqa

            preds = dbnet(imgs)
            assert preds.size(1) == 3

            _batch = torch.stack(
                [prob_maps, supervision_masks, threshold_maps, text_area_maps])
            prob_loss, threshold_loss, binary_loss, prob_threshold_loss, total_loss = criterion(  # noqa
                preds, _batch)
            db_optimizer.zero_grad()

            # prob_loss, threshold_loss, binary_loss, total_loss
            total_loss.backward()
            db_optimizer.step()
            if lrs_mode == 'poly':
                db_scheduler.step()

            # acc iou: pred_prob_map, gt_prob_map, supervision map, 0.3
            score_shrink_map = cal_text_score(
                preds[:, 0, :, :],
                prob_maps,
                supervision_masks,
                running_metric_text,
                thred=cfg.metric.thred_text_score)

            train_loss += total_loss
            acc = score_shrink_map['Mean Acc']
            iou_shrink_map = score_shrink_map['Mean IoU']

            # tf-board
            tfb_writer.add_scalar('TRAIN/LOSS/total_loss', total_loss,
                                  global_steps)
            tfb_writer.add_scalar('TRAIN/LOSS/loss', prob_threshold_loss,
                                  global_steps)
            tfb_writer.add_scalar('TRAIN/LOSS/prob_loss', prob_loss,
                                  global_steps)
            tfb_writer.add_scalar('TRAIN/LOSS/threshold_loss', threshold_loss,
                                  global_steps)
            tfb_writer.add_scalar('TRAIN/LOSS/binary_loss', binary_loss,
                                  global_steps)
            tfb_writer.add_scalar('TRAIN/ACC_IOU/acc', acc, global_steps)
            tfb_writer.add_scalar('TRAIN/ACC_IOU/iou_shrink_map',
                                  iou_shrink_map, global_steps)
            tfb_writer.add_scalar('TRAIN/HPs/lr', lr, global_steps)

            if global_steps % cfg.hps.log_iter == 0:
                logger.info("[{}-{}] - lr: {} - loss: {} - acc: {} - iou: {}".
                            format(  # noqa
                                epoch + 1, global_steps, lr, total_loss, acc,
                                iou_shrink_map))

        end_epoch_loss = train_loss / len(totaltext_train_loader)
        logger.info("Train loss: {}".format(end_epoch_loss))
        gc.collect()

        # TFB IMGs
        prob_threshold = cfg.metric.prob_threshold
        visualize_tfb(tfb_writer,
                      imgs,
                      preds,
                      global_steps=global_steps,
                      prob_threshold=prob_threshold,
                      mode="TRAIN")

        # EVAL
        dbnet.eval()
        test_loss = 0
        test_visualize_index = random.choice(range(len(totaltext_test_loader)))
        for test_batch_index, test_batch in tqdm(
                enumerate(totaltext_test_loader),
                total=len(totaltext_test_loader)):

            with torch.no_grad():
                test_batch = to_device(test_batch, device=device)
                img_fps, imgs, prob_maps, supervision_masks, threshold_maps, text_area_maps = test_batch  # noqa

                test_preds = dbnet(imgs)
                assert test_preds.size(1) == 2

                _batch = torch.stack([
                    prob_maps, supervision_masks, threshold_maps,
                    text_area_maps
                ])
                test_total_loss = criterion(test_preds, _batch)
                test_loss += test_total_loss

                # visualize predicted image with tfb
                if test_batch_index == test_visualize_index:
                    visualize_tfb(tfb_writer,
                                  imgs,
                                  test_preds,
                                  global_steps=global_steps,
                                  prob_threshold=prob_threshold,
                                  mode="TEST")

                test_score_shrink_map = cal_text_score(
                    test_preds[:, 0, :, :],
                    prob_maps,
                    supervision_masks,
                    running_metric_text,
                    thred=cfg.metric.thred_text_score)
                test_acc = test_score_shrink_map['Mean Acc']
                test_iou_shrink_map = test_score_shrink_map['Mean IoU']
                tfb_writer.add_scalar('TEST/LOSS/val_loss', test_total_loss,
                                      global_steps)
                tfb_writer.add_scalar('TEST/ACC_IOU/val_acc', test_acc,
                                      global_steps)
                tfb_writer.add_scalar('TEST/ACC_IOU/val_iou_shrink_map',
                                      test_iou_shrink_map, global_steps)

        test_loss = test_loss / len(totaltext_test_loader)
        logger.info("[{}] - test_loss: {}".format(global_steps, test_loss))

        if test_loss <= best_test_loss and train_loss < best_train_loss:
            best_test_loss = test_loss
            best_train_loss = train_loss
            torch.save(dbnet.state_dict(),
                       os.path.join(cfg.meta.root_dir, cfg.model.best_cp_path))

        if lrs_mode == 'reduce':
            db_scheduler.step(test_loss)
        torch.cuda.empty_cache()
        gc.collect()

    logger.info("Training completed")
    torch.save(dbnet.state_dict(),
               os.path.join(cfg.meta.root_dir, cfg.model.last_cp_path))
    logger.info("Saved model")


@hydra.main(config_path="../config.yaml", strict=False)
def run(cfg):
    main(cfg)


if __name__ == '__main__':
    run()
