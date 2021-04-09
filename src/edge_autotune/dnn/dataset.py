#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import io
import os

import numpy as np
import pandas as pd
import tensorflow.compat.v1 as tf

from PIL import Image
from object_detection.utils import dataset_util
from collections import namedtuple

from edge_autotune.dnn.tools import label_to_id_map

def _split_by_filename(
    df: DataFrame):
    """Split detections in DataFrame by filename.

    Args:
        df (DataFrame): DataFrame with detections

    Returns:
        list: TODO
    """
    data = namedtuple('data', ['filename', 'object'])
    gb = df.groupby('filename')
    return [data(filename, gb.get_group(x)) for filename, x in zip(gb.groups.keys(), gb.groups)]


def create_tf_example(group, path, id_map):
    with tf.gfile.GFile(os.path.join(path, '{}'.format(group.filename)), 'rb') as fid:
        encoded_jpg = fid.read()
    encoded_jpg_io = io.BytesIO(encoded_jpg)
    image = Image.open(encoded_jpg_io)
    width, height = image.size

    filename = group.filename.encode('utf8')
    image_format = b'jpg'
    xmins = []
    xmaxs = []
    ymins = []
    ymaxs = []
    classes_text = []
    classes = []

    for index, row in group.object.iterrows():
        xmins.append((row['xmin'] / width))
        xmaxs.append((row['xmax'] / width))
        ymins.append((row['ymin'] / height))
        ymaxs.append((row['ymax'] / height))
        classes_text.append(row['class'].encode('utf8'))
        classes.append(id_map[row['class']])

    if max(xmins + xmaxs + ymins + ymaxs) > 1.1:
        import pdb; pdb.set_trace()
    if min(xmins + xmaxs + ymins + ymaxs) < 0.0:
        import pdb; pdb.set_trace()
    assert max(xmins + xmaxs + ymins + ymaxs) <= 1.1
    assert min(xmins + xmaxs + ymins + ymaxs) >= 0.0

    tf_example = tf.train.Example(features=tf.train.Features(feature={
        'image/height': dataset_util.int64_feature(height),
        'image/width': dataset_util.int64_feature(width),
        'image/filename': dataset_util.bytes_feature(filename),
        'image/source_id': dataset_util.bytes_feature(filename),
        'image/encoded': dataset_util.bytes_feature(encoded_jpg),
        'image/format': dataset_util.bytes_feature(image_format),
        'image/object/bbox/xmin': dataset_util.float_list_feature(xmins),
        'image/object/bbox/xmax': dataset_util.float_list_feature(xmaxs),
        'image/object/bbox/ymin': dataset_util.float_list_feature(ymins),
        'image/object/bbox/ymax': dataset_util.float_list_feature(ymaxs),
        'image/object/class/text': dataset_util.bytes_list_feature(classes_text),
        'image/object/class/label': dataset_util.int64_list_feature(classes),
    }))
    return tf_example


def generate_tfrecord(
    output_path:str,
    images_dir: str,
    csv_input: str,
    label_map: dict):
    """Generate TFRecord dataset with images from the images_dir and detections from the input csv.

    Args:
        output_path (str): Path to the output TFRecord file.
        images_dir (str): Path to the directory that contains the images to write into the TFRecord file.
        csv_input (str): Path to the csv files with the detections to write into the TFRecord file.
        label_map (dict): label_map with pbtxt format. Defaults to None.
    """

    writer = tf.python_io.TFRecordWriter(output_path)
    path = os.path.join(images_dir)
    examples = pd.read_csv(csv_input)
    grouped = _split_by_filename(examples)
    id_map = label_to_id_map(label_map)
    for group in grouped:
        tf_example = create_tf_example(group, path, id_map)
        writer.write(tf_example.SerializeToString())

    writer.close()
    output_path = os.path.join(os.getcwd(), output_path)
    print('Successfully created the TFRecords: {}'.format(output_path))


def generate_joint_tfrecord(
    output_path: str,
    images_dirs: str,
    csv_inputs: list,
    label_map: dict = None):
    """Generate TFRecord dataset from a list of csv files with detections.

    Args:
        output_path (str): Path to the output TFRecord file.
        images_dirs (list): List of paths to the directories that contain the images to write into the TFRecord file.  
        csv_inputs (list): List of csv files with the detections to write into the TFRecord file. 
        label_map (dict, optional): label_map with pbtxt format. Defaults to None.
    """
    writer = tf.python_io.TFRecordWriter(output_path)
    id_map = label_to_id_map(label_map)
    for img_dir, csv in zip(images_dirs, csv_inputs):
        examples = pd.read_csv(csv)
        grouped = _split_by_filename(examples)
        path = os.path.join(img_dir)
        for group in grouped:
            tf_example = create_tf_example(group, path, id_map)
            writer.write(tf_example.SerializeToString())

    writer.close()
    output_path = os.path.join(os.getcwd(), output_path)
    print('Successfully created the TFRecords: {}'.format(output_path))