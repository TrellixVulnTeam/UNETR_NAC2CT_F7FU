import argparse
import cv2
import glob
import time
import random
import numpy as np
import torch.nn as nn
import nibabel as nib
from collections import OrderedDict
from sys import getsizeof
import os
import torch
import requests

from models.network_swinir import SwinIR as net
from utils import util_calculate_psnr_ssim as util

np.random.seed(seed=813)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', type=str, default='color_dn', help='classical_sr, lightweight_sr, real_sr, '
                                                                     'gray_dn, color_dn, jpeg_car')
    parser.add_argument('--scale', type=int, default=1, help='scale factor: 1, 2, 3, 4, 8') # 1 for dn and jpeg car
    parser.add_argument('--noise', type=int, default=15, help='noise level: 15, 25, 50')
    parser.add_argument('--jpeg', type=int, default=40, help='scale factor: 10, 20, 30, 40')
    parser.add_argument('--training_patch_size', type=int, default=128, help='patch size used in training SwinIR. '
                                       'Just used to differentiate two different settings in Table 2 of the paper. '
                                       'Images are NOT tested patch by patch.')
    parser.add_argument('--large_model', action='store_true', help='use large model, only provided for real image sr')
    parser.add_argument('--model_path', type=str, default='model_zoo/swinir/005_colorDN_DFWB_s128w8_SwinIR-M_noise25.pth')
    parser.add_argument('--folder_lq', type=str, default=None, help='input low-quality test image folder')
    parser.add_argument('--folder_gt', type=str, default=None, help='input ground-truth test image folder')
    parser.add_argument('--tile', type=int, default=None, help='Tile size, None for no tile during testing (testing as a whole)')
    parser.add_argument('--tile_overlap', type=int, default=32, help='Overlapping of different tiles')
    
    parser.add_argument('--gpu_ids', type=str, default="2", help='Use which GPU to train')
    parser.add_argument('--epoch', type=int, default=100, help='how many epochs to train')
    parser.add_argument('--batch', type=int, default=1, help='how many batches in one run')
    parser.add_argument('--loss_display_per_iter', type=int, default=600, help='display how many losses per iteration')
    parser.add_argument('--folder_pet', type=str, default="./trainsets/X/train/", help='input folder of T1MAP images')
    parser.add_argument('--folder_sct', type=str, default="./trainsets/Y/train/", help='input folder of BRAVO images')
    parser.add_argument('--folder_pet_v', type=str, default="./trainsets/X/val/", help='input folder of T1MAP PET images')
    parser.add_argument('--folder_sct_v', type=str, default="./trainsets/Y/val/", help='input folder of BRAVO images')
    parser.add_argument('--folder_pet_te', type=str, default="./trainsets/X/test/", help='input folder of T1MAP PET images')
    parser.add_argument('--folder_sct_te', type=str, default="./trainsets/Y/test/", help='input folder of BRAVO images')
    parser.add_argument('--weights_path', type=str, default='saved_models/model_best_021.pth')
    args = parser.parse_args()

    gpu_list = ','.join(str(x) for x in args.gpu_ids)
    os.environ['CUDA_VISIBLE_DEVICES'] = gpu_list
    print('export CUDA_VISIBLE_DEVICES=' + gpu_list)

    device = torch.device('cuda' if  torch.cuda.is_available()else 'cpu')
    # set up model
    if os.path.exists(args.model_path):
        print(f'loading model from {args.model_path}')
    else:
        os.makedirs(os.path.dirname(args.model_path), exist_ok=True)
        url = 'https://github.com/JingyunLiang/SwinIR/releases/download/v0.0/{}'.format(os.path.basename(args.model_path))
        r = requests.get(url, allow_redirects=True)
        print(f'downloading model {args.model_path}')
        open(args.model_path, 'wb').write(r.content)

    model = define_model(args)
    model.eval().float()
    model = model.to(device)
    
    sct_list = sorted(glob.glob(args.folder_sct+"*.npy"))
    # criterion_list = [nn.L1Loss, nn.MSELoss, nn.SmoothL1Loss]
    criterion_list = []
    # (nii_file, loss)
    loss_mat = np.zeros((len(sct_list), len(criterion_list)))

    for cnt_sct, sct_path in enumerate(sct_list):

        cube_x_path = sct_path.replace("Y", "X")
        cube_y_path = sct_path
        print("--->",cube_x_path,"<---", end="")
        cube_x_data = np.load(cube_x_path)
        assert cube_x_data.shape == cube_y_data.shape
        len_z = cube_x_data.shape[1]
        y_hat = np.zeros(cube_y_data.shape)
        
        for idx in range(len_z):

            batch_x = np.zeros((1, 3, cube_x_data.shape[0], cube_x_data.shape[2]))
            batch_y = cube_y_data[:, idx, :]

            z_center = idx
            batch_x[0, 1, :, :] = cube_x_data[:, z_center, :]
            z_before = z_center - 1 if z_center > 0 else 0
            z_after = z_center + 1 if z_center < len_z-1 else len_z-1
            batch_x[0, 0, :, :] = cube_x_data[:, z_before, :]
            batch_x[0, 2, :, :] = cube_x_data[:, z_after, :]

            batch_x = torch.from_numpy(batch_x).float().to(device)

            y_hat[:, idx, :] = np.squeeze(model(batch_x).numpy())
        
        for cnt_loss, loss_fnc in enumerate(criterion_list):
            curr_loss = loss_fnc(cube_y_data, y_hat).item()
            loss_mat[cnt_sct, cnt_loss] = curr_loss
            print("===> Loss[{}]: {:6}".format(loss_fnc.__name__, curr_loss), end='')
        
        file_idx = os.path.basename(sct_path)[4:7]
        nifty_name = "mets" if file_idx[0] == "0" else "tami"
        nifty_name = nifty_name + "000" + file_idx[1:] + ".nii.gz"
        nifty_name = "./t1map2bravo/T1MAP/" + nifty_name
        nifty_file = nib.load(nifty_file)
        print("Loaded from ", nifty_name, end="")

        pred_file = nib.Nifti1Image(y_hat, nifty_file.affine, nifty_file.header)
        pred_name = "./t1map2bravo/pred/"+"PRD_"+file_idx+".nii.gz"
        nib.save(pred_file, pred_name)
        print("Saved to ", pred_name)
















    # for idx, path in enumerate(sorted(glob.glob(os.path.join(folder, '*')))):
    #     # read image
    #     imgname, img_lq, img_gt = get_image_pair(args, path)  # image to HWC-BGR, float32
    #     img_lq = np.transpose(img_lq if img_lq.shape[2] == 1 else img_lq[:, :, [2, 1, 0]], (2, 0, 1))  # HCW-BGR to CHW-RGB
    #     img_lq = torch.from_numpy(img_lq).float().unsqueeze(0).to(device)  # CHW-RGB to NCHW-RGB

    #     # inference
    #     with torch.no_grad():
    #         # pad input image to be a multiple of window_size
    #         _, _, h_old, w_old = img_lq.size()
    #         h_pad = (h_old // window_size + 1) * window_size - h_old
    #         w_pad = (w_old // window_size + 1) * window_size - w_old
    #         img_lq = torch.cat([img_lq, torch.flip(img_lq, [2])], 2)[:, :, :h_old + h_pad, :]
    #         img_lq = torch.cat([img_lq, torch.flip(img_lq, [3])], 3)[:, :, :, :w_old + w_pad]
    #         output = test(img_lq, model, args, window_size)
    #         output = output[..., :h_old * args.scale, :w_old * args.scale]

    #     # save image
    #     output = output.data.squeeze().float().cpu().clamp_(0, 1).numpy()
    #     if output.ndim == 3:
    #         output = np.transpose(output[[2, 1, 0], :, :], (1, 2, 0))  # CHW-RGB to HCW-BGR
    #     # output = (output * 255.0).round().astype(np.uint8)  # float32 to uint8
    #     # cv2.imwrite(f'{save_dir}/{imgname}_SwinIR.png', output)
    #     np.save(f'{save_dir}/{imgname}_SwinIR.npy', output)

    #     # evaluate psnr/ssim/psnr_b
    #     if img_gt is not None:
    #         img_gt = (img_gt * 255.0).round().astype(np.uint8)  # float32 to uint8
    #         img_gt = img_gt[:h_old * args.scale, :w_old * args.scale, ...]  # crop gt
    #         img_gt = np.squeeze(img_gt)

    #         psnr = util.calculate_psnr(output, img_gt, crop_border=border)
    #         ssim = util.calculate_ssim(output, img_gt, crop_border=border)
    #         test_results['psnr'].append(psnr)
    #         test_results['ssim'].append(ssim)
    #         if img_gt.ndim == 3:  # RGB image
    #             psnr_y = util.calculate_psnr(output, img_gt, crop_border=border, test_y_channel=True)
    #             ssim_y = util.calculate_ssim(output, img_gt, crop_border=border, test_y_channel=True)
    #             test_results['psnr_y'].append(psnr_y)
    #             test_results['ssim_y'].append(ssim_y)
    #         if args.task in ['jpeg_car']:
    #             psnr_b = util.calculate_psnrb(output, img_gt, crop_border=border, test_y_channel=True)
    #             test_results['psnr_b'].append(psnr_b)
    #         print('Testing {:d} {:20s} - PSNR: {:.2f} dB; SSIM: {:.4f}; '
    #               'PSNR_Y: {:.2f} dB; SSIM_Y: {:.4f}; '
    #               'PSNR_B: {:.2f} dB.'.
    #               format(idx, imgname, psnr, ssim, psnr_y, ssim_y, psnr_b))
    #     else:
    #         print('Testing {:d} {:20s}'.format(idx, imgname))

    # # summarize psnr/ssim
    # if img_gt is not None:
    #     ave_psnr = sum(test_results['psnr']) / len(test_results['psnr'])
    #     ave_ssim = sum(test_results['ssim']) / len(test_results['ssim'])
    #     print('\n{} \n-- Average PSNR/SSIM(RGB): {:.2f} dB; {:.4f}'.format(save_dir, ave_psnr, ave_ssim))
    #     if img_gt.ndim == 3:
    #         ave_psnr_y = sum(test_results['psnr_y']) / len(test_results['psnr_y'])
    #         ave_ssim_y = sum(test_results['ssim_y']) / len(test_results['ssim_y'])
    #         print('-- Average PSNR_Y/SSIM_Y: {:.2f} dB; {:.4f}'.format(ave_psnr_y, ave_ssim_y))
    #     if args.task in ['jpeg_car']:
    #         ave_psnr_b = sum(test_results['psnr_b']) / len(test_results['psnr_b'])
    #         print('-- Average PSNR_B: {:.2f} dB'.format(ave_psnr_b))


def define_model(args):
    # 001 classical image sr
    if args.task == 'classical_sr':
        model = net(upscale=args.scale, in_chans=3, img_size=args.training_patch_size, window_size=8,
                    img_range=1., depths=[6, 6, 6, 6, 6, 6], embed_dim=180, num_heads=[6, 6, 6, 6, 6, 6],
                    mlp_ratio=2, upsampler='pixelshuffle', resi_connection='1conv')
        param_key_g = 'params'

    # 002 lightweight image sr
    # use 'pixelshuffledirect' to save parameters
    elif args.task == 'lightweight_sr':
        model = net(upscale=args.scale, in_chans=3, img_size=64, window_size=8,
                    img_range=1., depths=[6, 6, 6, 6], embed_dim=60, num_heads=[6, 6, 6, 6],
                    mlp_ratio=2, upsampler='pixelshuffledirect', resi_connection='1conv')
        param_key_g = 'params'

    # 003 real-world image sr
    elif args.task == 'real_sr':
        if not args.large_model:
            # use 'nearest+conv' to avoid block artifacts
            model = net(upscale=4, in_chans=3, img_size=64, window_size=8,
                        img_range=1., depths=[6, 6, 6, 6, 6, 6], embed_dim=180, num_heads=[6, 6, 6, 6, 6, 6],
                        mlp_ratio=2, upsampler='nearest+conv', resi_connection='1conv')
        else:
            # larger model size; use '3conv' to save parameters and memory; use ema for GAN training
            model = net(upscale=4, in_chans=3, img_size=64, window_size=8,
                        img_range=1., depths=[6, 6, 6, 6, 6, 6, 6, 6, 6], embed_dim=240,
                        num_heads=[8, 8, 8, 8, 8, 8, 8, 8, 8],
                        mlp_ratio=2, upsampler='nearest+conv', resi_connection='3conv')
        param_key_g = 'params_ema'

    # 004 grayscale image denoising
    elif args.task == 'gray_dn':
        model = net(upscale=1, in_chans=1, img_size=128, window_size=8,
                    img_range=1., depths=[6, 6, 6, 6, 6, 6], embed_dim=180, num_heads=[6, 6, 6, 6, 6, 6],
                    mlp_ratio=2, upsampler='', resi_connection='1conv')
        param_key_g = 'params'

    # 005 color image denoising
    elif args.task == 'color_dn':
        model = net(upscale=1, in_chans=3, img_size=128, window_size=8,
                    img_range=1., depths=[6, 6, 6, 6, 6, 6], embed_dim=180, num_heads=[6, 6, 6, 6, 6, 6],
                    mlp_ratio=2, upsampler='', resi_connection='1conv')
        param_key_g = 'params'

    # 006 JPEG compression artifact reduction
    # use window_size=7 because JPEG encoding uses 8x8; use img_range=255 because it's sligtly better than 1
    elif args.task == 'jpeg_car':
        model = net(upscale=1, in_chans=1, img_size=126, window_size=7,
                    img_range=255., depths=[6, 6, 6, 6, 6, 6], embed_dim=180, num_heads=[6, 6, 6, 6, 6, 6],
                    mlp_ratio=2, upsampler='', resi_connection='1conv')
        param_key_g = 'params'

    pretrained_model = torch.load(args.weights_path)
    model.load_state_dict(pretrained_model[param_key_g] if param_key_g in pretrained_model.keys() else pretrained_model, strict=True)

    return model


def setup(args):
    # 001 classical image sr/ 002 lightweight image sr
    if args.task in ['classical_sr', 'lightweight_sr']:
        save_dir = f'results/swinir_{args.task}_x{args.scale}'
        folder = args.folder_gt
        border = args.scale
        window_size = 8

    # 003 real-world image sr
    elif args.task in ['real_sr']:
        save_dir = f'results/swinir_{args.task}_x{args.scale}'
        if args.large_model:
            save_dir += '_large'
        folder = args.folder_lq
        border = 0
        window_size = 8

    # 004 grayscale image denoising/ 005 color image denoising
    elif args.task in ['gray_dn', 'color_dn']:
        save_dir = f'results/swinir_{args.task}_noise{args.noise}'
        folder = args.folder_gt
        border = 0
        window_size = 8

    # 006 JPEG compression artifact reduction
    elif args.task in ['jpeg_car']:
        save_dir = f'results/swinir_{args.task}_jpeg{args.jpeg}'
        folder = args.folder_gt
        border = 0
        window_size = 7

    return folder, save_dir, border, window_size


def get_image_pair(args, path):
    (imgname, imgext) = os.path.splitext(os.path.basename(path))

    # 001 classical image sr/ 002 lightweight image sr (load lq-gt image pairs)
    if args.task in ['classical_sr', 'lightweight_sr']:
        # img_gt = cv2.imread(path, cv2.IMREAD_COLOR).astype(np.float32) / 255.
        # img_lq = cv2.imread(f'{args.folder_lq}/{imgname}x{args.scale}{imgext}', cv2.IMREAD_COLOR).astype(
        #     np.float32) / 255.
        img_gt = np.load(path)
        # img_gt = None
        img_lq = np.load(f'{args.folder_lq}/{imgname}{imgext}')

    # 003 real-world image sr (load lq image only)
    elif args.task in ['real_sr']:
        img_gt = None
        # img_lq = cv2.imread(path, cv2.IMREAD_COLOR).astype(np.float32) / 255.
        img_lq = np.load(path)


    # 004 grayscale image denoising (load gt image and generate lq image on-the-fly)
    elif args.task in ['gray_dn']:
        img_gt = cv2.imread(path, cv2.IMREAD_GRAYSCALE).astype(np.float32) / 255.
        np.random.seed(seed=0)
        img_lq = img_gt + np.random.normal(0, args.noise / 255., img_gt.shape)
        img_gt = np.expand_dims(img_gt, axis=2)
        img_lq = np.expand_dims(img_lq, axis=2)

    # 005 color image denoising (load gt image and generate lq image on-the-fly)
    elif args.task in ['color_dn']:
        img_gt = cv2.imread(path, cv2.IMREAD_COLOR).astype(np.float32) / 255.
        np.random.seed(seed=0)
        img_lq = img_gt + np.random.normal(0, args.noise / 255., img_gt.shape)

    # 006 JPEG compression artifact reduction (load gt image and generate lq image on-the-fly)
    elif args.task in ['jpeg_car']:
        img_gt = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img_gt.ndim != 2:
            img_gt = util.rgb2ycbcr(cv2.cvtColor(img_gt, cv2.COLOR_BGR2RGB), y_only=True)
        result, encimg = cv2.imencode('.jpg', img_gt, [int(cv2.IMWRITE_JPEG_QUALITY), args.jpeg])
        img_lq = cv2.imdecode(encimg, 0)
        img_gt = np.expand_dims(img_gt, axis=2).astype(np.float32) / 255.
        img_lq = np.expand_dims(img_lq, axis=2).astype(np.float32) / 255.

    return imgname, img_lq, img_gt


def test(img_lq, model, args, window_size):
    if args.tile is None:
        # test the image as a whole
        output = model(img_lq)
    else:
        # test the image tile by tile
        b, c, h, w = img_lq.size()
        tile = min(args.tile, h, w)
        assert tile % window_size == 0, "tile size should be a multiple of window_size"
        tile_overlap = args.tile_overlap
        sf = args.scale

        stride = tile - tile_overlap
        h_idx_list = list(range(0, h-tile, stride)) + [h-tile]
        w_idx_list = list(range(0, w-tile, stride)) + [w-tile]
        E = torch.zeros(b, c, h*sf, w*sf).type_as(img_lq)
        W = torch.zeros_like(E)

        for h_idx in h_idx_list:
            for w_idx in w_idx_list:
                in_patch = img_lq[..., h_idx:h_idx+tile, w_idx:w_idx+tile]
                out_patch = model(in_patch)
                out_patch_mask = torch.ones_like(out_patch)

                E[..., h_idx*sf:(h_idx+tile)*sf, w_idx*sf:(w_idx+tile)*sf].add_(out_patch)
                W[..., h_idx*sf:(h_idx+tile)*sf, w_idx*sf:(w_idx+tile)*sf].add_(out_patch_mask)
        output = E.div_(W)

    return output

if __name__ == '__main__':
    main()
