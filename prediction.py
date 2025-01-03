"""  
Copyright (c) 2019-present NAVER Corp.
MIT License
"""

# -*- coding: utf-8 -*-
import sys
import os
import time
import argparse

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.autograd import Variable

from PIL import Image

import cv2
from skimage import io
import numpy as np
import craft_utils
import imgproc
import file_utils
import json
import zipfile

from craft import CRAFT

from collections import OrderedDict
def copyStateDict(state_dict):
    if list(state_dict.keys())[0].startswith("module"):
        start_idx = 1
    else:
        start_idx = 0
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        name = ".".join(k.split(".")[start_idx:])
        new_state_dict[name] = v
    return new_state_dict

def str2bool(v):
    return v.lower() in ("yes", "y", "true", "t", "1")


trained_model = 'weights/craft_mlt_25k.pth'
text_threshold = 0.7
low_text = 0.4
link_threshold = 0.4
cuda = True
canvas_size = 1280
mag_ratio = 1.5
poly = False
show_time = False
test_folder = '/data/'
refine = False
refiner_model = 'weights/craft_refiner_CTW1500.pth'

""" For test images in a folder """
# image_list, _, _ = file_utils.get_files(test_folder)

result_folder = './result/'
if not os.path.isdir(result_folder):
    os.mkdir(result_folder)

def test_net(net, image, text_threshold, link_threshold, low_text, cuda, poly, refine_net=None):
    t0 = time.time()

    # resize
    img_resized, target_ratio, size_heatmap = imgproc.resize_aspect_ratio(image, canvas_size, interpolation=cv2.INTER_LINEAR, mag_ratio=mag_ratio)
    ratio_h = ratio_w = 1 / target_ratio

    # preprocessing
    x = imgproc.normalizeMeanVariance(img_resized)
    x = torch.from_numpy(x).permute(2, 0, 1)    # [h, w, c] to [c, h, w]
    x = Variable(x.unsqueeze(0))                # [c, h, w] to [b, c, h, w]
    if cuda:
        x = x.cuda()

    # forward pass
    with torch.no_grad():
        y, feature = net(x)

    # make score and link map
    score_text = y[0,:,:,0].cpu().data.numpy()
    score_link = y[0,:,:,1].cpu().data.numpy()

    # refine link
    if refine_net is not None:
        with torch.no_grad():
            y_refiner = refine_net(y, feature)
        score_link = y_refiner[0,:,:,0].cpu().data.numpy()

    t0 = time.time() - t0
    t1 = time.time()

    # Post-processing
    boxes, polys = craft_utils.getDetBoxes(score_text, score_link, text_threshold, link_threshold, low_text, poly)

    # coordinate adjustment
    boxes = craft_utils.adjustResultCoordinates(boxes, ratio_w, ratio_h)
    polys = craft_utils.adjustResultCoordinates(polys, ratio_w, ratio_h)
    for k in range(len(polys)):
        if polys[k] is None: polys[k] = boxes[k]

    t1 = time.time() - t1

    # render results (optional)
    render_img = score_text.copy()
    render_img = np.hstack((render_img, score_link))
    ret_score_text = imgproc.cvt2HeatmapImg(render_img)

    if show_time : print("\ninfer/postproc time : {:.3f}/{:.3f}".format(t0, t1))

    return boxes, polys, ret_score_text, (score_text, score_link)



class Access():
    def __init__(self, trained_model="weights/craft_mlt_25k.pth"):
        self.setup(trained_model)
    
    def setup(self, trained_model):
        # load net
        self.net = CRAFT()     # initialize

        print('Loading weights from checkpoint (' + trained_model + ')')
        if cuda:
            self.net.load_state_dict(copyStateDict(torch.load(trained_model)))
        else:
            self.net.load_state_dict(copyStateDict(torch.load(trained_model, map_location='cpu')))

        if cuda:
            self.net = self.net.cuda()
            self.net = torch.nn.DataParallel(self.net)
            cudnn.benchmark = False

        self.net.eval()

        # LinkRefiner
        self.refine_net = None
        if refine:
            from refinenet import RefineNet
            self.refine_net = RefineNet()
            print('Loading weights of refiner from checkpoint (' + refiner_model + ')')
            if cuda:
                self.refine_net.load_state_dict(copyStateDict(torch.load(refiner_model)))
                self.refine_net = self.refine_net.cuda()
                self.refine_net = torch.nn.DataParallel(self.refine_net)
            else:
                self.refine_net.load_state_dict(copyStateDict(torch.load(refiner_model, map_location='cpu')))

            self.refine_net.eval()
            poly = True
    
    def predict(self, image_s):
        image = image_s if not isinstance(image_s, str) else imgproc.loadImage(image_s)

        bboxes, polys, score_text, output = test_net(self.net, image, text_threshold, link_threshold, low_text, cuda, poly, self.refine_net)
        return bboxes, polys, score_text, output
    
    def export_onx(self):
        # Check if the model is wrapped in DataParallel
        model_to_export = self.net.module if isinstance(self.net, torch.nn.DataParallel) else self.net

        # Move model to evaluation mode
        model_to_export.eval()

        # Move input to the same device as the model
        device = next(model_to_export.parameters()).device  # Get the device of the model
        dummy_input = torch.randn(1, 3, 768, 768).to(device)  # Move input to the same device

        # Export to ONNX
        torch.onnx.export(
            model_to_export,
            dummy_input,
            "craft_model.onnx",
            input_names=["input"],
            output_names=["output", "feature"],
            dynamic_axes={"input": {0: "batch_size"}, "output": {0: "batch_size"}},
            opset_version=11
        )
        print("Model exported to ONNX!")
