#!/usr/bin/env python3
import os
import cv2
import numpy as np
from pathlib import Path
from PIL import Image, ExifTags
import argparse

def load_image(path):
    img = Image.open(path)
    try:
        for orientation in ExifTags.TAGS.keys():
            if ExifTags.TAGS[orientation] == 'Orientation':
                break
        exif = img._getexif()
        if exif is not None:
            orient = exif.get(orientation)
            if orient == 3:
                img = img.rotate(180, expand=True)
            elif orient == 6:
                img = img.rotate(270, expand=True)
            elif orient == 8:
                img = img.rotate(90, expand=True)
    except Exception:
        pass
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

def save_image(folder, name, img):
    folder.mkdir(parents=True, exist_ok=True)
    out_path = folder / name
    ext = out_path.suffix.lower()
    if img.ndim == 2:
        if ext not in ('.png',):
            out_path = out_path.with_suffix('.png')
        cv2.imwrite(str(out_path), img)
    else:
        if ext not in ('.jpg', '.jpeg'):
            out_path = out_path.with_suffix('.jpg')
        cv2.imwrite(str(out_path), img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

def preprocess_and_save_all_stages(in_path, out_root, max_size=2048):
    """
    Pipeline:
      1. CLAHE (histogram_equalized)
      2. Gaussian Blur (gaussian_blur)
      3. Unsharp Mask (sharpened)
      4. Edge Detection (edges)
      5. Median Filter (median_filtered)
      6. Morphological Cleaning (morphology)
      7. Final Processed (combined sharpened + median + morphology)
    """
    img = load_image(str(in_path))
    h, w = img.shape[:2]
    if max(h, w) > max_size:
        scale = max_size / float(max(h, w))
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    name = in_path.name
    stem = in_path.stem

    # Convert to YCrCb for luminance processing
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    y, cr, cb = cv2.split(ycc)

    # 1. CLAHE (contrast enhancement)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
    y_eq = clahe.apply(y)
    img_eq = cv2.cvtColor(cv2.merge([y_eq, cr, cb]), cv2.COLOR_YCrCb2BGR)
    save_image(out_root / "histogram_equalized", name, img_eq)

    # 2. Gaussian Blur
    y_blur = cv2.GaussianBlur(y_eq, (5,5), 0)
    img_blur = cv2.cvtColor(cv2.merge([y_blur, cr, cb]), cv2.COLOR_YCrCb2BGR)
    save_image(out_root / "gaussian_blur", name, img_blur)

    # 3. Unsharp Mask (Sharpening)
    sharpen_amount = 0.3  
    blurred = cv2.GaussianBlur(y_blur, (3,3), 0)
    y_sharp = cv2.addWeighted(y_blur, 1.0 + sharpen_amount, blurred, -sharpen_amount, 0)
    y_sharp = cv2.bilateralFilter(y_sharp, d=5, sigmaColor=25, sigmaSpace=25)
    img_sharp = cv2.cvtColor(cv2.merge([y_sharp, cr, cb]), cv2.COLOR_YCrCb2BGR)
    save_image(out_root / "sharpened", name, img_sharp)

    # 4. Edge Detection (Canny)
    gray = cv2.cvtColor(img_sharp, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 100, 200)
    save_image(out_root / "edges", f"{stem}_edges.png", edges)

    # 5. Median Filter
    median = cv2.medianBlur(gray, 5)
    save_image(out_root / "median_filtered", f"{stem}_median.png", median)

    # 6. Morphological Cleaning (Opening + Closing)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3,3))
    morph_open = cv2.morphologyEx(median, cv2.MORPH_OPEN, kernel, iterations=1)
    morph_clean = cv2.morphologyEx(morph_open, cv2.MORPH_CLOSE, kernel, iterations=1)
    save_image(out_root / "morphology", f"{stem}_morph.png", morph_clean)

    # 7. Final Processed (combine sharpened + morphological mask)
    alpha = 0.75  
    beta = 0.25 
    y_final = cv2.addWeighted(y_sharp, alpha, morph_clean, beta, 0)
    final_bgr = cv2.cvtColor(cv2.merge([y_final, cr, cb]), cv2.COLOR_YCrCb2BGR)
    save_image(out_root / "final_processed", name, final_bgr)
