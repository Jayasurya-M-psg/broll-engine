import json
import os
import subprocess
import urllib.request
import sys

def run_merge():
    print("[1/5] Initializing merge pipeline...")
    work_dir = "/tmp/broll_merge"
    os.makedirs(work_dir, exist_ok=True)

    # 4 scenes from Job 2953cdc2c6c1
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
            "duration": 4.04 # matches the 12.35s narration audio
        }
    ]

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # Step 2: Download raw clips
    print("[2/5] Downloading stock footage clips from Pixabay...")
    processed_16x9 = []
    processed_9x16 = []

    for item in clips_info:
        raw_path = os.path.join(work_dir, f"raw_{item['id']}.mp4")
        proc_16x9 = os.path.join(work_dir, f"proc_16x9_{item['id']}.mp4")
        proc_9x16 = os.path.join(work_dir, f"proc_9x16_{item['id']}.mp4")

        if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 10000:
            print(f"  Downloading clip {item['id']} ({item['duration']}s)...")
            req = urllib.request.Request(item["url"], headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp, open(raw_path, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            print(f"  Saved raw clip {item['id']} ({os.path.getsize(raw_path)/(1024*1024):.2f} MB)")

        # Normalize 16:9 (1920x1080)
        print(f"  Processing 16:9 for clip {item['id']}...")
        cmd_16x9 = [
            "ffmpeg", "-y",
            "-i", raw_path,
            "-t", str(item["duration"]),
            "-vf", (
                "scale=1920:1080:force_original_aspect_ratio=increase,"
                "crop=1920:1080,"
                "setsar=1,"
                "eq=brightness=0.02:contrast=1.05:saturation=1.1,"
                "fps=30"
            ),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-an",
            proc_16x9
        ]
        subprocess.run(cmd_16x9, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processed_16x9.append(proc_16x9)

        # Normalize 9:16 (1080x1920)
        print(f"  Processing 9:16 for clip {item['id']}...")
        cmd_9x16 = [
            "ffmpeg", "-y",
            "-i", raw_path,
            "-t", str(item["duration"]),
            "-vf", (
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,"
                "setsar=1,"
                "eq=brightness=0.02:contrast=1.05:saturation=1.1,"
                "fps=30"
            ),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-an",
            proc_9x16
        ]
        subprocess.run(cmd_9x16, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processed_9x16.append(proc_9x16)

    # Step 3: Audio check / conversion
    print("[3/5] Preparing narration audio track...")
    audio_wav = "/tmp/narration.wav"
    audio_mp3 = os.path.join(work_dir, "narration.mp3")
    if not os.path.exists(audio_wav):
        print("  Narration WAV not found at /tmp/narration.wav, falling back to espeak-ng...")
        full_script = " ".join(item["text"] for item in clips_info)
        subprocess.run(["espeak-ng", "-v", "en-us+f3", "-s", "140", "-w", audio_wav, full_script], check=True)
    
    subprocess.run(["ffmpeg", "-y", "-i", audio_wav, "-c:a", "libmp3lame", "-b:a", "192k", audio_mp3], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Step 4: Concatenate video clips
    print("[4/5] Concatenating clips...")
    concat_16x9_txt = os.path.join(work_dir, "concat_16x9.txt")
    with open(concat_16x9_txt, "w") as f:
        for p in processed_16x9:
            f.write(f"file '{p}'\n")

    video_16x9_no_audio = os.path.join(work_dir, "video_16x9_no_audio.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_16x9_txt,
        "-c", "copy",
        video_16x9_no_audio
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    concat_9x16_txt = os.path.join(work_dir, "concat_9x16.txt")
    with open(concat_9x16_txt, "w") as f:
        for p in processed_9x16:
            f.write(f"file '{p}'\n")

    video_9x16_no_audio = os.path.join(work_dir, "video_9x16_no_audio.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_9x16_txt,
        "-c", "copy",
        video_9x16_no_audio
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Step 5: Muxing audio to create final videos
    print("[5/5] Muxing narration audio with merged video...")
    final_16x9 = os.path.join(work_dir, "final_video.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_16x9_no_audio,
        "-i", audio_mp3,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        final_16x9
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    final_9x16 = os.path.join(work_dir, "final_video_9x16.mp4")
    subprocess.run([
        "ffmpeg", "-y",
        "-i", video_9x16_no_audio,
        "-i", audio_mp3,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        final_9x16
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Generate SRT captions
    srt_path = os.path.join(work_dir, "captions.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,000 --> 00:00:03,690\nSaving money does not have to be difficult.\n\n")
        f.write("2\n00:00:03,690 --> 00:00:06,000\nStart by tracking every expense.\n\n")
        f.write("3\n00:00:06,000 --> 00:00:08,310\nThen cut one unnecessary subscription.\n\n")
        f.write("4\n00:00:08,310 --> 00:00:12,340\nSmall steps lead to big savings over time.\n\n")

    print(f"SUCCESS! 16:9 merged video: {final_16x9} ({os.path.getsize(final_16x9)/(1024*1024):.2f} MB)")
    print(f"SUCCESS! 9:16 merged video: {final_9x16} ({os.path.getsize(final_9x16)/(1024*1024):.2f} MB)")

if __name__ == "__main__":
    run_merge()
