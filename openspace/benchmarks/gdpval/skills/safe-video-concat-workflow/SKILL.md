---
name: safe-video-concat-workflow
description: Two-pass video concatenation workflow that avoids encoder compatibility issues by separating concat and audio mixing
---

# Safe Video Concatenation Workflow

This workflow prevents encoder compatibility failures when creating video showreels or compilations by using a two-pass approach: concatenate video clips first, then add music/SFX in a separate pass.

## When to Use

- Creating video showreels from multiple source clips
- Adding background music or sound effects to concatenated videos
- Avoiding encoder errors when combining clips with audio mixing

## Step 1: Probe Source Videos

Before processing, verify resolution and duration of all source clips using ffprobe:

```bash
# Get video resolution
ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 clip.mp4

# Get video duration
ffprobe -v error -show_entries format=duration -of csv=p=0 clip.mp4
```

Record the resolution (e.g., 1920x1080) to determine if stream copy is safe.

## Step 2: Use Stream Copy When Resolution Matches

If source clips already match your target resolution, use `-c:v copy` to avoid re-encoding:

```bash
# Good: avoids encoder issues when resolution matches
-c:v copy

# Only re-encode if resolution conversion is needed
-c:v libx264 -s 1920x1080
```

This prevents encoder compatibility failures that can occur with complex filter chains.

## Step 3: Build Concat List with Bare Filenames

Create the concat list file with **bare filenames only** (no directory prefixes):

```
file 'clip1.mp4'
file 'clip2.mp4'
file 'clip3.mp4'
```

**Important:** Run ffmpeg from the same directory as the video clips, or the concat demuxer may fail to find files.

```bash
cd /path/to/clips
ffmpeg -f concat -safe 0 -i file_list.txt ...
```

## Step 4: Two-Pass Processing

### Pass 1: Concatenate Video Clips

Concatenate all clips first into an intermediate file:

```bash
ffmpeg -f concat -safe 0 -i file_list.txt -c:v copy -c:a aac -y interim.mp4
```

### Pass 2: Add Music/SFX with Complex Filter

Apply audio mixing or other complex filters in a separate pass:

```bash
ffmpeg -i interim.mp4 -i background_music.mp3 \
  -filter_complex "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=3" \
  -c:v copy -c:a aac -y final_output.mp4
```

## Why Two Passes?

Combining concatenation and audio mixing in a single command can cause:
- Encoder compatibility failures
- Resource exhaustion
- Complex filter chain errors

The two-pass approach isolates concerns and ensures each operation completes successfully.

## Quick Reference Commands

```bash
# Probe resolution
ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 VIDEO.mp4

# Create concat list (bare filenames, run from clip directory)
echo "file 'clip1.mp4'" > concat_list.txt
echo "file 'clip2.mp4'" >> concat_list.txt

# Pass 1: Concatenate
ffmpeg -f concat -safe 0 -i concat_list.txt -c:v copy -c:a aac -y interim.mp4

# Pass 2: Add audio
ffmpeg -i interim.mp4 -i music.mp3 -filter_complex "[0:a][1:a]amix=inputs=2" -c:v copy -y output.mp4
```