import os
import subprocess
import argparse
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed

# Constants
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.heic', '.webp', '.tiff', '.bmp', '.arw', '.tga'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v'}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

def get_file_metadata(filepath):
    """Worker function: Extracts creation date and size using macOS 'mdls'."""
    try:
        size = os.path.getsize(filepath)
        ext = os.path.splitext(filepath)[1].lower()
        is_video = ext in VIDEO_EXTS

        # Use mdls for high-accuracy 'Date Taken'
        cmd = ['mdls', '-name', 'kMDItemContentCreationDate', '-raw', filepath]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8')
        
        dt_str = None
        if output and "(null)" not in output:
            # Format: 2023-05-12 14:30:15 +0000
            dt_str = output[:7] # YYYY-MM
            
        return {
            "month": dt_str,
            "size": size,
            "is_video": is_video
        }
    except Exception:
        return None

def format_size(bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes < 1024.0:
            return f"{bytes:.2f} {unit}"
        bytes /= 1024.0

def analyze_library(folders):
    print(f"[*] Starting Library Analysis across {len(folders)} locations...")
    print(f"[*] This may take a few minutes for 100K+ files. Please wait...")

    # 1. Collect all media file paths
    all_paths = []
    for folder in folders:
        for root, _, files in os.walk(folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in MEDIA_EXTS:
                    all_paths.append(os.path.join(root, f))
    
    total_files = len(all_paths)
    print(f"[*] Found {total_files} media files. Starting parallel metadata extraction...")

    # 2. Parallel Processing
    stats = defaultdict(lambda: {"img_count": 0, "img_size": 0, "vid_count": 0, "vid_size": 0})
    no_exif = {"img_count": 0, "img_size": 0, "vid_count": 0, "vid_size": 0}

    # Using 8 workers for a good balance on Mac
    processed = 0
    with ProcessPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_file_metadata, p): p for p in all_paths}
        
        for future in as_completed(futures):
            res = future.result()
            if res:
                if res['month']:
                    m = res['month']
                    if res['is_video']:
                        stats[m]["vid_count"] += 1
                        stats[m]["vid_size"] += res['size']
                    else:
                        stats[m]["img_count"] += 1
                        stats[m]["img_size"] += res['size']
                else:
                    if res['is_video']:
                        no_exif["vid_count"] += 1
                        no_exif["vid_size"] += res['size']
                    else:
                        no_exif["img_count"] += 1
                        no_exif["img_size"] += res['size']
            
            processed += 1
            if processed % 1000 == 0 or processed == total_files:
                print(f"    - Processed {processed}/{total_files} files...", end='\r')

    # 3. Output Summary
    print("\n\n" + "="*60)
    print(f"{'MONTH':<10} | {'IMAGES':<20} | {'VIDEOS':<20}")
    print("-" * 60)
    
    sorted_months = sorted(stats.keys(), reverse=True)
    for m in sorted_months:
        s = stats[m]
        img_str = f"{s['img_count']} ({format_size(s['img_size'])})"
        vid_str = f"{s['vid_count']} ({format_size(s['vid_size'])})"
        print(f"{m:<10} | {img_str:<20} | {vid_str:<20}")

    print("-" * 60)
    print(f"{'No EXIF':<10} | "
          f"{no_exif['img_count']} ({format_size(no_exif['img_size'])}) | "
          f"{no_exif['vid_count']} ({format_size(no_exif['vid_size'])})")
    
    print("="*60)
    print(f"Total files with EXIF: {total_files - (no_exif['img_count'] + no_exif['vid_count'])}")
    print(f"Total files WITHOUT EXIF: {no_exif['img_count'] + no_exif['vid_count']}")
    print("="*60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="High-performance Library Metadata analyzer for macOS.")
    parser.add_argument("folders", nargs='+', help="Folders to analyze.")
    args = parser.parse_args()
    
    analyze_library(args.folders)
