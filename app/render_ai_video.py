import os
import subprocess
import urllib.request

def render_ai_video():
    print("[1/5] Setting up extended duration AI video rendering pipeline...")
    work_dir = "/tmp/ai_broll"
    os.makedirs(work_dir, exist_ok=True)

    # 4 scenes with extended duration based on script (24.0s total)
    clips_info = [
        {
            "id": 0,
            "text": "Artificial Intelligence is changing the world faster than ever before.",
            "url": "https://cdn.pixabay.com/video/2020/06/24/43022-434334062_medium.mp4",
            "duration": 5.0,
        },
        {
            "id": 1,
            "text": "From smart assistants to self-driving cars, AI is making everyday tasks easier.",
            "url": "https://cdn.pixabay.com/video/2022/06/10/119848-719283265_medium.mp4",
            "duration": 7.0,
        },
        {
            "id": 2,
            "text": "In the future, people and machines will work together to solve complex problems.",
            "url": "https://cdn.pixabay.com/video/2021/09/11/88219-602915845_medium.mp4",
            "duration": 7.0,
        },
        {
            "id": 3,
            "text": "The key is to keep learning and adapt to new technology.",
            "url": "https://cdn.pixabay.com/video/2024/12/18/247478_medium.mp4",
            "duration": 5.0,
        },
    ]

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    proc_16x9_list = []
    proc_9x16_list = []

    print("[2/5] Downloading and normalizing exact motion clips with extended duration...")
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

        # 16:9 Landscape
        print(f"  Trimming & scaling 16:9 for clip {item['id']} ({item['duration']}s)...")
        cmd_16x9 = [
            "ffmpeg", "-y",
            "-i", raw_path,
            "-t", str(item["duration"]),
            "-vf", (
                "scale=1920:1080:force_original_aspect_ratio=increase,"
                "crop=1920:1080,"
                "setsar=1,"
                "eq=brightness=0.02:contrast=1.06:saturation=1.12,"
                "fps=30"
            ),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an",
            proc_16x9,
        ]
        subprocess.run(cmd_16x9, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc_16x9_list.append(proc_16x9)

        # 9:16 Portrait
        print(f"  Trimming & scaling 9:16 for clip {item['id']} ({item['duration']}s)...")
        cmd_9x16 = [
            "ffmpeg", "-y",
            "-i", raw_path,
            "-t", str(item["duration"]),
            "-vf", (
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,"
                "setsar=1,"
                "eq=brightness=0.02:contrast=1.06:saturation=1.12,"
                "fps=30"
            ),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an",
            proc_9x16,
        ]
        subprocess.run(cmd_9x16, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc_9x16_list.append(proc_9x16)

    # Audio track
    print("[3/5] Syncing extended narration audio track...")
    audio_wav = "/tmp/ai_narration_extended.wav"
    audio_mp3 = os.path.join(work_dir, "ai_narration_extended.mp3")
    subprocess.run([
        "ffmpeg", "-y", "-i", audio_wav,
        "-c:a", "libmp3lame", "-b:a", "192k",
        audio_mp3,
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Concat clips
    print("[4/5] Concatenating scenes into video tracks...")
    concat_16x9_txt = os.path.join(work_dir, "concat_16x9.txt")
    with open(concat_16x9_txt, "w") as f:
        for p in proc_16x9_list:
            f.write(f"file '{p}'\n")

    v16x9_raw = os.path.join(work_dir, "v16x9_raw.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_16x9_txt, "-c", "copy", v16x9_raw], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    concat_9x16_txt = os.path.join(work_dir, "concat_9x16.txt")
    with open(concat_9x16_txt, "w") as f:
        for p in proc_9x16_list:
            f.write(f"file '{p}'\n")

    v9x16_raw = os.path.join(work_dir, "v9x16_raw.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_9x16_txt, "-c", "copy", v9x16_raw], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Mux Audio
    final_16x9 = os.path.join(work_dir, "ai_video_16x9.mp4")
    subprocess.run(["ffmpeg", "-y", "-i", v16x9_raw, "-i", audio_mp3, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", final_16x9], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    final_9x16 = os.path.join(work_dir, "ai_video_9x16.mp4")
    subprocess.run(["ffmpeg", "-y", "-i", v9x16_raw, "-i", audio_mp3, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", final_9x16], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Subtitles SRT
    print("[5/5] Generating synchronized subtitles & burning captions...")
    srt_path = os.path.join(work_dir, "captions.srt")
    with open(srt_path, "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,000 --> 00:00:05,000\nArtificial Intelligence is changing the world faster than ever before.\n\n")
        f.write("2\n00:00:05,000 --> 00:00:12,000\nFrom smart assistants to self-driving cars, AI is making everyday tasks easier.\n\n")
        f.write("3\n00:00:12,000 --> 00:00:19,000\nIn the future, people and machines will work together to solve complex problems.\n\n")
        f.write("4\n00:00:19,000 --> 00:00:24,000\nThe key is to keep learning and adapt to new technology.\n\n")

    # Burn captions onto 16:9
    final_16x9_captioned = os.path.join(work_dir, "ai_video_16x9_captioned.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-i", final_16x9,
        "-vf", "subtitles=" + srt_path + ":force_style='FontSize=26,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=3,Outline=3,MarginV=50'",
        "-c:a", "copy",
        final_16x9_captioned,
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Burn captions onto 9:16
    final_9x16_captioned = os.path.join(work_dir, "ai_video_9x16_captioned.mp4")
    subprocess.run([
        "ffmpeg", "-y", "-i", final_9x16,
        "-vf", "subtitles=" + srt_path + ":force_style='FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=3,Outline=3,MarginV=180'",
        "-c:a", "copy",
        final_9x16_captioned,
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("SUCCESS! Extended duration video (24.0s) rendered successfully!")

if __name__ == "__main__":
    render_ai_video()
