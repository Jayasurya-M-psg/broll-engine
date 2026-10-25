import json
import os
import subprocess
import urllib.request
import tempfile

def merge_all():
    manifest_path = "app/demo_manifest.json"
    
    # 4 scenes from our successful demo
    clips_info = [
        {
            "id": 0,
            "text": "Saving money does not have to be difficult.",
            "url": "https://cdn.pixabay.com/video/2022/02/28/109146-683077298_medium.mp4",
            "duration": 3.69
        },
        {
            "id": 1,
            "text": "Start by tracking every expense.",
            "url": "https://cdn.pixabay.com/video/2020/05/24/39990-424360898_medium.mp4",
            "duration": 2.31
        },
        {
            "id": 2,
            "text": "Then cut one unnecessary subscription.",
            "url": "https://cdn.pixabay.com/video/2020/09/02/48891-454346906_medium.mp4",
            "duration": 2.31
        },
        {
            "id": 3,
            "text": "Small steps lead to big savings over time.",
            "url": "https://cdn.pixabay.com/video/2016/05/12/3171-166338953_medium.mp4",
            "duration": 3.69
        }
    ]

    work_dir = "/tmp/broll_merge"
    os.makedirs(work_dir, exist_ok=True)
    
    headers = {"User-Agent": "Mozilla/5.0"}
    processed_clips = []

    print("[1/4] Downloading stock footage clips...")
    for item in clips_info:
        raw_path = os.path.join(work_dir, f"raw_{item['id']}.mp4")
        proc_path = os.path.join(work_dir, f"proc_{item['id']}.mp4")
        
        if not os.path.exists(raw_path):
            print(f"  Downloading clip {item['id']}...")
            req = urllib.request.Request(item["url"], headers=headers)
            with urllib.request.urlopen(req) as resp, open(raw_path, "wb") as f:
                f.write(resp.read())
        
        print(f"  Trimming & normalizing clip {item['id']} to {item['duration']}s (1920x1080 @ 30fps)...")
        # FFmpeg scale + pad to 1920x1080, set 30 fps, trim to exact duration
        cmd = [
            "ffmpeg", "-y",
            "-i", raw_path,
            "-t", str(item["duration"]),
            "-vf", (
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,"
                "eq=brightness=0.02:contrast=1.05:saturation=1.1,"
                "fps=30"
            ),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-an",
            proc_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processed_clips.append(proc_path)

    print("[2/4] Generating speech narration audio with espeak-ng...")
    full_script = " ".join(item["text"] for item in clips_info)
    narration_wav = os.path.join(work_dir, "narration.wav")
    narration_mp3 = os.path.join(work_dir, "narration.mp3")
    
    subprocess.run(["espeak-ng", "-v", "en-us+f3", "-s", "140", "-w", narration_wav, full_script], check=True)
    subprocess.run(["ffmpeg", "-y", "-i", narration_wav, narration_mp3], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("[3/4] Merging all clips into one continuous video...")
    concat_list = os.path.join(work_dir, "concat.txt")
    with open(concat_list, "w") as f:
        for p in processed_clips:
            f.write(f"file '{p}'\n")

    merged_no_audio = os.path.join(work_dir, "merged_no_audio.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_list,
        "-c", "copy",
        merged_no_audio
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("[4/4] Muxing narration audio with the merged video...")
    final_output = "/app/app/final_merged_video.mp4"
    subprocess.run([
        "ffmpeg", "-y",
        "-i", merged_no_audio,
        "-i", narration_mp3,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        final_output
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"SUCCESS! Merged video generated at: {final_output}")
    print(f"File size: {os.path.getsize(final_output) / (1024*1024):.2f} MB")

if __name__ == "__main__":
    merge_all()
