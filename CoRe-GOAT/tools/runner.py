from scipy import stats
from tools import builder, helper
from tools.trainer import Trainer
import time
from models.cnn_model import GCNnet_artisticswimming
from models.group_aware_attention import Encoder_Blocks
from utils.multi_gpu import *
from models.cnn_simplified import GCNnet_artisticswimming_simplified
from models.linear_for_bp import Linear_For_Backbone
from thop import profile

import mindspore as ms
import mindspore.nn as nn
import mindspore.ops as ops

def test_net(args):
    print('Tester start ... ')
    train_dataset, test_dataset = builder.dataset_builder(args)
    column_names = ["data"]
    for i in range(args.voter_number):
        column_names.append("target" + str(i))
    test_dataloader = ms.dataset.GeneratorDataset(test_dataset,
                                                  column_names=column_names,
                                                  num_parallel_workers=int(args.workers),
                                                  shuffle=False).batch(batch_size=args.bs_test)
    base_model, regressor = builder.model_builder(args)
    # load checkpoints
    builder.load_model(base_model, regressor, args)

    # if using RT, build a group
    group = builder.build_group(train_dataset, args)

    # CUDA
    global use_gpu
    use_gpu = True

    #  DP
    # base_model = nn.DataParallel(base_model)
    # regressor = nn.DataParallel(regressor)

    test(base_model, regressor, test_dataloader, group, args)


def run_net(args):
    if is_main_process():
        print('Trainer start ... ')
    # build dataset
    train_dataset, test_dataset = builder.dataset_builder(args)
    if args.use_multi_gpu:
        train_dataloader = build_dataloader(train_dataset,
                                            batch_size=args.bs_train,
                                            shuffle=True,
                                            num_workers=args.workers,
                                            persistent_workers=True,
                                            seed=set_seed(args.seed))
    else:
        train_dataloader = ms.dataset.GeneratorDataset(train_dataset,
                                                       column_names=["data", "target"],
                                                       num_parallel_workers=args.workers,
                                                       shuffle=False).batch(batch_size=args.bs_train)
    column_names = ["data"]
    for i in range(args.voter_number):
        column_names.append("target" + str(i))
    test_dataloader = ms.dataset.GeneratorDataset(test_dataset,
                                                  column_names=column_names,
                                                  num_parallel_workers=args.workers,
                                                  shuffle=False).batch(batch_size=args.bs_test)

    # Set data position
    device = get_device()

    # build model
    base_model, regressor = builder.model_builder(args)

    input1 = ops.randn(2, 2049)
    # flops, params = profile(regressor, inputs=(input1, ))
    # print(f'[regressor]flops: ', flops, 'params: ', params)

    if args.warmup:
        num_steps = len(train_dataloader) * args.max_epoch
        # lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=num_steps)
        lr_scheduler = nn.cosine_decay_lr(1e-5, 0.1, num_steps, len(train_dataloader), args.max_epoch // 2)
    
    # Set models and optimizer(depend on whether to use goat)
    if args.use_goat:
        if args.use_cnn_features:
            gcn = GCNnet_artisticswimming_simplified(args)

            input1 = ops.randn(1, 540, 8, 1024)
            input2 = ops.randn(1, 540, 8, 4)
            flops, params = profile(gcn, inputs=(input1, input2))
            print(f'[GCNnet_artisticswimming_simplified]flops: ', flops, 'params: ', params)
        else:
            gcn = GCNnet_artisticswimming(args)
            gcn.loadmodel(args.stage1_model_path)
        attn_encoder = Encoder_Blocks(args.qk_dim, 1024, args.linear_dim, args.num_heads, args.num_layers, args.attn_drop)
        linear_bp = Linear_For_Backbone(args)
        optimizer = nn.Adam([
            {'params': gcn.trainable_params(), 'lr': args.lr * args.lr_factor},
            {'params': regressor.trainable_params()},
            {'params': linear_bp.trainable_params()},
            {'params': attn_encoder.trainable_params()}
        ], learning_rate=lr_scheduler if args.warmup else args.lr, weight_decay=args.weight_decay)
        scheduler = None
        if args.use_multi_gpu:
            wrap_model(gcn, distributed=args.distributed)
            wrap_model(attn_encoder, distributed=args.distributed)
            wrap_model(linear_bp, distributed=args.distributed)
            wrap_model(regressor, distributed=args.distributed)
        else:
            gcn = gcn
            attn_encoder = attn_encoder
            linear_bp = linear_bp
            regressor = regressor
    else:
        gcn = None
        attn_encoder = None
        linear_bp = Linear_For_Backbone(args)
        optimizer = nn.Adam([{'params': regressor.trainable_params()}, {'params': linear_bp.trainable_params()}], learning_rate=lr_scheduler if args.warmup else args.lr, weight_decay=args.weight_decay)
        scheduler = None
        if args.use_multi_gpu:
            wrap_model(regressor, distributed=args.distributed)
            wrap_model(linear_bp, distributed=args.distributed)
        else:
            regressor = regressor
            linear_bp = linear_bp


    # if using RT, build a group
    group = builder.build_group(train_dataset, args)
    # CUDA
    # global use_gpu
    # use_gpu = torch.cuda.is_available()
    # if use_gpu:
    #     torch.backends.cudnn.benchmark = True

    ms.set_context(device_target='GPU', device_id=0)

    # parameter setting
    start_epoch = 0
    global epoch_best, rho_best, L2_min, RL2_min
    epoch_best = 0
    rho_best = 0
    L2_min = 1000
    RL2_min = 1000

    # resume ckpts
    if args.resume:
        start_epoch, epoch_best, rho_best, L2_min, RL2_min = \
            builder.resume_train(base_model, regressor, optimizer, args)
        print('resume ckpts @ %d epoch( rho = %.4f, L2 = %.4f , RL2 = %.4f)' % (
            start_epoch - 1, rho_best, L2_min, RL2_min))

    #  DP
    # regressor = nn.DataParallel(regressor)
    # if args.use_goat:
    #     gcn = nn.DataParallel(gcn)
    #     attn_encoder = nn.DataParallel(attn_encoder)

    # loss
    mse = nn.MSELoss()
    nll = nn.NLLLoss()
    
    trainer = Trainer(base_model, regressor, group, mse, nll, optimizer, args, gcn, attn_encoder, linear_bp)

    # trainval

    # training
    for epoch in range(start_epoch, args.max_epoch):
        if args.use_multi_gpu:
            train_dataloader.sampler.set_epoch(epoch)
        true_scores = []
        pred_scores = []
        num_iter = 0
        # base_model.train()  # set model to training mode
        trainer.set_train()
        # if args.fix_bn:
        #     base_model.apply(misc.fix_bn)  # fix bn
        for idx, (data, target) in enumerate(train_dataloader):
            if args.bs_train == 1:
                data = {k: v.unsqueeze(0) for k, v in data.items() if k != 'key'}
                target = {k: v.unsqueeze(0) for k, v in target.items() if k != 'key'}

            # break
            num_iter += 1
            opti_flag = False

            true_scores.extend(data['final_score'].numpy())
            # data preparing
            # featue_1 is the test video ; video_2 is exemplar
            if args.benchmark == 'MTL':
                feature_1 = data['feature'].float()  # N, C, T, H, W
                if args.usingDD:
                    label_1 = data['completeness'].float().reshape(-1, 1)
                    label_2 = target['completeness'].float().reshape(-1, 1)
                else:
                    label_1 = data['final_score'].float().reshape(-1, 1)
                    label_2 = target['final_score'].float().reshape(-1, 1)
                if not args.dive_number_choosing and args.usingDD:
                    assert (data['difficulty'] == target['difficulty']).all()
                diff = data['difficulty'].float().reshape(-1, 1)
                feature_2 = target['feature'].float()  # N, C, T, H, W

            else:
                raise NotImplementedError()

            # forward
            if num_iter == args.step_per_update:
                num_iter = 0
                opti_flag = True
            
            # helper.network_forward_train(base_model, regressor, pred_scores, feature_1, label_1, feature_2, label_2,
            #                              diff, group, mse, nll, optimizer, opti_flag, epoch, idx + 1,
            #                              len(train_dataloader), args, data, target, gcn, attn_encoder, linear_bp)
            start = time.time()
            loss, leaf_probs_2, delta_2 = trainer.train_epoch(feature_1, label_1, feature_2, label_2, data, target, opti_flag)
            end = time.time()
            batch_time = end - start
            batch_idx = idx + 1
            if batch_idx % args.print_freq == 0:
                print('[Training][%d/%d][%d/%d] \t Batch_time %.2f \t Batch_loss: %.4f \t'
                      % (epoch, args.max_epoch, batch_idx, len(train_dataloader),
                         batch_time, loss.item()))

            # evaluate result of training phase
            relative_scores = group.inference(leaf_probs_2.numpy(), delta_2.numpy())
            if args.benchmark == 'MTL':
                if args.usingDD:
                    score = (relative_scores + label_2) * diff
                else:
                    score = relative_scores + label_2
            elif args.benchmark == 'Seven':
                score = relative_scores + label_2
            else:
                raise NotImplementedError()
            pred_scores.extend(score.numpy())

        # analysis on results
        pred_scores = np.array(pred_scores).squeeze()
        true_scores = np.array(true_scores)
        rho, p = stats.spearmanr(pred_scores, true_scores)
        L2 = np.power(pred_scores - true_scores, 2).sum() / true_scores.shape[0]
        RL2 = np.power((pred_scores - true_scores) / (true_scores.max() - true_scores.min()), 2).sum() / \
              true_scores.shape[0]
        if is_main_process():
            print('[Training] EPOCH: %d, correlation: %.4f, L2: %.4f, RL2: %.4f' % (
                epoch, rho, L2, RL2))

        if is_main_process():
            trainer.set_test()
            validate(trainer.base_model, trainer.regressor, test_dataloader, epoch, trainer.group, args, trainer.gcn, trainer.attn_encoder, trainer.linear_bp)
            # helper.save_checkpoint(base_model, regressor, optimizer, epoch, epoch_best, rho_best, L2_min, RL2_min,
            #                        'last',
            #                        args)
            print('[TEST] EPOCH: %d, best correlation: %.6f, best L2: %.6f, best RL2: %.6f' % (
                epoch, rho_best, L2_min, RL2_min))
        # scheduler lr
        if scheduler is not None:
            scheduler.step()


# TODO: 修改以下所有;修改['difficulty'].float
def validate(base_model, regressor, test_dataloader, epoch, group, args, gcn, attn_encoder, linear_bp):
    print("Start validating epoch {}".format(epoch))
    global use_gpu
    global epoch_best, rho_best, L2_min, RL2_min
    true_scores = []
    pred_scores = []
    # base_model.eval()  # set model to eval mode
    batch_num = len(test_dataloader)

    datatime_start = time.time()
    for batch_idx, data_list in enumerate(test_dataloader, 0):
        data = data_list[0]
        target = data_list[1:]
        if args.bs_test == 1:
            data = {k: v.unsqueeze(0) for k, v in data.items() if k != 'key'}
            for i in range(len(target)):
                target[i] = {k: v.unsqueeze(0) for k, v in target[i].items() if k != 'key'}
        datatime = time.time() - datatime_start
        start = time.time()
        true_scores.extend(data['final_score'].numpy())
        # data prepare
        if args.benchmark == 'MTL':
            feature_1 = data['feature'].float()  # N, C, T, H, W
            if args.usingDD:
                label_2_list = [item['completeness'].float().reshape(-1, 1) for item in target]
            else:
                label_2_list = [item['final_score'].float().reshape(-1, 1) for item in target]
            diff = data['difficulty'].float().reshape(-1, 1)
            feature_2_list = [item['feature'].float() for item in target]
            # check
            if not args.dive_number_choosing and args.usingDD:
                for item in target:
                    assert (diff == item['difficulty'].reshape(-1, 1)).all()
        else:
            raise NotImplementedError()
        helper.network_forward_test(base_model, regressor, pred_scores, feature_1, feature_2_list, label_2_list,
                                    diff, group, args, data, target, gcn, attn_encoder, linear_bp)
        batch_time = time.time() - start
        if batch_idx % args.print_freq == 0:
            print('[TEST][%d/%d][%d/%d] \t Batch_time %.6f \t Data_time %.6f '
                  % (epoch, args.max_epoch, batch_idx, batch_num, batch_time, datatime))
        datatime_start = time.time()

    # analysis on results
    pred_scores = np.array(pred_scores).squeeze()
    true_scores = np.array(true_scores)
    rho, p = stats.spearmanr(pred_scores, true_scores)
    L2 = np.power(pred_scores - true_scores, 2).sum() / true_scores.shape[0]
    RL2 = np.power((pred_scores - true_scores) / (true_scores.max() - true_scores.min()), 2).sum() / \
          true_scores.shape[0]
    if L2_min > L2:
        L2_min = L2
    if RL2_min > RL2:
        RL2_min = RL2
    if rho > rho_best:
        rho_best = rho
        epoch_best = epoch
        print('-----New best found!-----')
        # helper.save_outputs(pred_scores, true_scores, args)
        # helper.save_checkpoint(base_model, regressor, optimizer, epoch, epoch_best, rho_best, L2_min, RL2_min,
        #                        'best', args)
    if epoch == args.max_epoch - 1:
        log_best(rho_best, RL2_min, epoch_best, args)

    print('[TEST] EPOCH: %d, correlation: %.6f, L2: %.6f, RL2: %.6f' % (epoch, rho, L2, RL2))


def test(base_model, regressor, test_dataloader, group, args, gcn, attn_encoder):
    global use_gpu
    true_scores = []
    pred_scores = []
    # base_model.eval()  # set model to eval mode
    regressor.eval()
    if args.use_goat:
        gcn.eval()
        attn_encoder.eval()
    batch_num = len(test_dataloader)


    datatime_start = time.time()
    for batch_idx, (data, target) in enumerate(test_dataloader, 0):
        if args.bs_test == 1:
            data = {k: v.unsqueeze(0) for k, v in data.items() if k != 'key'}
            target = {k: v.unsqueeze(0) for k, v in target.items() if k != 'key'}
        datatime = time.time() - datatime_start
        start = time.time()
        true_scores.extend(data['final_score'].numpy())
        # data prepare
        if args.benchmark == 'MTL':
            featue_1 = data['feature'].float()  # N, C, T, H, W
            if args.usingDD:
                label_2_list = [item['completeness'].float().reshape(-1, 1) for item in target]
            else:
                label_2_list = [item['final_score'].float().reshape(-1, 1) for item in target]
            diff = data['difficulty'].float().reshape(-1, 1)
            feature_2_list = [item['feature'].float() for item in target]
            # check
            if not args.dive_number_choosing and args.usingDD:
                for item in target:
                    assert (diff == item['difficulty'].float().reshape(-1, 1)).all()
        elif args.benchmark == 'Seven':
            featue_1 = data['feature'].float()  # N, C, T, H, W
            feature_2_list = [item['feature'].float() for item in target]
            label_2_list = [item['final_score'].float().reshape(-1, 1) for item in target]
            diff = None
        else:
            raise NotImplementedError()
        helper.network_forward_test(base_model, regressor, pred_scores, featue_1, feature_2_list, label_2_list,
                                    diff, group, args, data, target, gcn, attn_encoder)
        batch_time = time.time() - start
        if batch_idx % args.print_freq == 0:
            print('[TEST][%d/%d] \t Batch_time %.2f \t Data_time %.2f '
                  % (batch_idx, batch_num, batch_time, datatime))
        datatime_start = time.time()

        # analysis on results
        pred_scores = np.array(pred_scores).squeeze()
        true_scores = np.array(true_scores)
        rho, p = stats.spearmanr(pred_scores, true_scores)
        L2 = np.power(pred_scores - true_scores, 2).sum() / true_scores.shape[0]
        RL2 = np.power((pred_scores - true_scores) / (true_scores.max() - true_scores.min()), 2).sum() / \
              true_scores.shape[0]
        print('[TEST] correlation: %.6f, L2: %.6f, RL2: %.6f' % (rho, L2, RL2))
