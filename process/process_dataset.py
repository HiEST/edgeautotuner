#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import product
import os
from pathlib import Path
import shutil
import sys
import time

import cv2
import numpy as np
import pandas as pd
import pickle
import torch
from torchvision.models import mobilenet_v2, resnet152
from torchvision.models.detection import faster_rcnn

sys.path.append('../')
from utils.detector import init_detector, run_detector
from utils.detector import ALL_MODELS as TF_MODELS

devices = {}
models = {}


def infer_tf(model, img, device='cpu'):
    ts0 = time.time()
    results = run_detector(model, img, model.input_size)
    ts1 = time.time()
    print(f'inference took {ts1-ts0:.2f} seconds.')
    return results


@torch.no_grad()
def infer_torch(model, img, device='cpu'):
    dev = torch.device('cpu') if device == 'cpu' else device
    model.to(dev)

    img = np.array(img)
    img = img.astype("single") / float(255)
    x = torch.from_numpy(img).permute(2, 0, 1).unsqueeze(0)
    x = x.to(dev)

    ts0 = time.time()
    predictions = model(x)
    del x
    ts1 = time.time()
    print(f'inference took {ts1-ts0:.2f} seconds.')

    return predictions[0].detach().cpu().numpy()


def get_top_torch(preds, topn=10):
    if topn > len(preds):
        topn = len(preds)
    idxs = np.argpartition(-preds, 5)[:topn]
    results = []
    for i, idx in enumerate(idxs):
        results.append([str(idx), f'{preds[idx]:.2f}'])

    top = {
        'idxs': results[0],
        'scores': results[1]
    }
    return top


def get_top_tf(preds, topn=10):
    boxes = preds['detection_boxes'][0]
    scores = preds['detection_scores'][0]
    class_ids = preds['detection_classes'][0]

    top = {
        'boxes': boxes[:topn].tolist(),
        'scores': scores[:topn].tolist(),
        'idxs': class_ids[:topn].astype(int).tolist()
    }
    return top


def infer_video(filename, model, device, framework):
    cap = cv2.VideoCapture(filename)
    ret, frame = cap.read()
    if ret:
        frame_shape = frame.shape
    else:
        frame_shape = (0, 0)
    frame_id = 0

    data = []
    while ret:
        if framework == 'torch':
            preds = infer_torch(model, frame, device)
            data.append(get_top_torch(preds))

        else:
            preds = infer_tf(model, frame, device)
            data.append(get_top_tf(preds))

        frame_id += 1
        ret, frame = cap.read()

    return data, frame_shape


def process_video(filename, model, device='cpu', framework='torch', output=None):
    ts0 = time.time()
    data, frame_shape = infer_video(filename, model, device, framework)
    if output is not None and len(data) > 0:
        path = f'{output}/{Path(filename).stem}.pkl'
        columns = ['frame', 'class_id', 'score', 'xmin', 'ymin', 'xmax', 'ymax']

        df_data = []
        for i,d in enumerate(data):
            for j in range(len(d['boxes'])):
                class_id = int(d['idxs'][j])
                score = float(d['scores'][j])
                bbox = d['boxes'][j]
                ymin, xmin, ymax, xmax = tuple(bbox)
                  
                (xmin, xmax, ymin, ymax) = (int(xmin * frame_shape[1]), int(xmax * frame_shape[1]),
                                              int(ymin * frame_shape[0]), int(ymax * frame_shape[0]))
                df_data.append([i, class_id, score, xmin, ymin, xmax, ymax])
                
        df = pd.DataFrame(df_data, columns=columns)
        df.to_pickle(path, compression='bz2')

    scores = [','.join(f'{s:.3f}'
                       for s in p['scores']) for p in data]
    classes = [','.join(str(c)
                        for c in p['idxs']) for p in data]

    rows = [[i, s, classes[i]] for i, s in enumerate(scores)]

    ts1 = time.time()
    print(f'Time to process video {filename} '
          f'for model {model}: {ts1-ts0:.2f} seconds.')
    return rows


def process_video_parallel(args):
    global devices
    global models

    filename = args[0]
    model = args[1]
    framework = args[2]
    output = args[3]

    m = models[model]
    device = devices[model]

    return process_video(filename, m, device, framework, output)


def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def split_date(ts):
    ts = pd.to_datetime(ts)
    date = str(ts.date())
    hour = str(ts.hour)
    minute = str(ts.minute)

    return date, hour, minute


def process_dataset(dataset, dataset_name,
                    model='edge',
                    framework='torch',
                    output='./',
                    per_video_results=False,
                    process_all=True,
                    move_when_done=None,
                    max_workers=1):

    columns = ['cam', 'timestamp', 'date', 'hour', 'minute', 'frame_id',
               'model', 'top_scores', 'top_classes']
    subcolumns = ['frame_id', 'top_scores', 'top_classes']

    detections = pd.DataFrame([], columns=columns)

    processed_ts = defaultdict(bool)

    videos_processed = defaultdict(int)
    videos = []
    if process_all:
        videos = [str(f) for f in dataset]
    else:
        for f in dataset:
            ts = f.stem.split('.')[0]
            cam = f.stem.split('.')[1]
            date, hour, minute = split_date(ts)

            date_hour = f'[{cam}] {date}:{hour}'
            if processed_ts[date_hour]:
                continue
            else:
                videos.append(str(f))
                processed_ts[date_hour] = f.stem

    if per_video_results:
        num_videos = len(videos)
        videos = [v for v in videos if not os.path.isfile(f'{output}/{Path(v).stem}.pkl')]
        print(f'Skipped {num_videos - len(videos)} videos because they were already processed')

    video_results_output = output if per_video_results else None
    query_args = [
        [video, model, framework, video_results_output]
        for video in videos
    ]
    # query_args = [
    #     [video, model, framework]
    #     for video, model in product(videos, models.keys())
    # ]
    if len(query_args) < max_workers*2:
        max_workers = len(query_args)

    # Check if there are previous *.tmp.csv.{id} files and start after latest id
    tmp_files = [int(str(tmp.suffix).split('.')[1]) for tmp in Path(f'{output}').glob('*.tmp.csv.*')]
    previous_tmp_files = 0
    if len(tmp_files) > 0:
        previous_tmp_files = max(tmp_files) + 1

    for chunk_idx, chunk in enumerate(chunks(query_args, max_workers*2)):
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            results = executor.map(process_video_parallel, chunk)
    
            for r_idx, r in enumerate(results):
                filename = chunk[r_idx][0]
                if len(r) == 0:
                    print(f'0 results for {filename} with model {model}')
                    continue
                ts = Path(filename).stem.split('.')[0]
                cam = Path(filename).stem.split('.')[1]
                date, hour, minute = split_date(ts)

                df = pd.DataFrame(r, columns=subcolumns)
                df['cam'] = cam
                df['model'] = model
                df['timestamp'] = ts
                df['date'] = date
                df['hour'] = hour
                df['minute'] = minute

                detections = detections.append(df, ignore_index=True)
                print(f'{len(detections)} detections.')
                print(f'Writing results to {dataset_name}.tmp.csv.{chunk_idx+previous_tmp_files}')
                detections.to_csv(f'{output}/{dataset_name}.tmp.csv.{chunk_idx+previous_tmp_files}',
                                  sep=',',
                                  float_format='.2f',
                                  index=False)

                print(f'Results from {filename} just processed.')

            videos_processed[filename] += 1
            if move_when_done is not None and \
                    videos_processed[filename] == len(models):
                shutil.move(filename, move_when_done)
                del videos_processed[filename]

    detections.to_csv(f'{output}/{dataset_name}.csv',
                      sep=',',
                      float_format='.2f',
                      index=False)


def main():
    global models
    global devices
    args = argparse.ArgumentParser()
    args.add_argument("-i", "--input",
                      default="./",
                      type=str,
                      help="Path to the dataset to process.")

    args.add_argument("-o", "--output",
                      default="./",
                      type=str,
                      help="Path where to write results.")

    args.add_argument("-n", "--name",
                      default="dataset",
                      type=str,
                      help="Name of the dataset. Used to name the results file.")

    args.add_argument("-m", "--model",
                      default='edge',
                      choices=['edge', 'ref'],
                      help="Model to use")

    args.add_argument("-f", "--framework",
                      default='torch',
                      choices=['torch', 'tf'],
                      help="Framework to use")

    args.add_argument("--fast",
                      default=False,
                      action="store_true",
                      help="Processes one video per hour "
                      "instead of the whole dataset.")

    args.add_argument("--per-video-results",
                      default=False,
                      action="store_true",
                      help="Write results per video."
                      "instead of the whole dataset.")

    args.add_argument("--move",
                      default=None,
                      type=str,
                      help="If specified, videos are moved "
                      "to this path after processing.")

    args.add_argument("--max-workers",
                      default=1,
                      type=int,
                      help="Max. workers to send parallel requests.")

    config = args.parse_args()

    path = Path(config.input)
    if os.path.isfile(config.input):
        dataset = [path]
    else:
        extensions = ['.mkv', '.mp4', '.webm']
        dataset = sorted([f for f in path.rglob('*') if f.suffix in extensions], key=os.path.getmtime)

    if config.framework == 'torch':
        models['edge'] = mobilenet_v2(pretrained=True)
        models['ref'] = resnet152(pretrained=True)

        devices = {}
        if torch.cuda.is_available():
            devices['edge'] = torch.device('cuda:0')
            devices['ref'] = torch.device('cuda:1')
        else:
            devices['edge'] = 'cpu'
            devices['ref'] = 'cpu'

        models['edge'].eval()
        models['ref'].eval()
    elif config.framework == 'tf':
        # Edge Model: MobileNetV2 320x320
        ref_model = 'Faster R-CNN Inception ResNet V2 1024x1024'

        # Load model
        if config.model == 'edge':
            models['edge'] = init_detector()
            models['edge'].input_size = (320, 320)
            devices['edge'] = 'cpu'
        else:
            models['ref'] = init_detector(ref_model)
            models['ref'].input_size = (1024, 1024)
            devices['ref'] = 'cpu'

    process_dataset(dataset,
                    dataset_name=config.name,
                    model=config.model,
                    framework=config.framework,
                    output=config.output,
                    per_video_results=config.per_video_results,
                    process_all=(not config.fast),
                    move_when_done=config.move,
                    max_workers=config.max_workers)


if __name__ == '__main__':
    main()
