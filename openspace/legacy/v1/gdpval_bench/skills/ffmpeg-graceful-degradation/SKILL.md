---
name: ffmpeg-graceful-degradation
description: Graceful degradation workflow for ffmpeg encoding failures with progressive fallback strategies
---

# FFmpeg Graceful Degradation

When processing videos with ffmpeg, encoding failures are common due to codec availability, library version mismatches, or system configuration issues. This skill provides a systematic fallback strategy to ensure video processing completes successfully.

## Overview

The pattern involves: (1) probing encoder availability upfront, (2) testing on a short clip before batch processing, (3) progressive fallback through copy mode, alternative codecs, and finally moviepy, (4) using moviepy as a reliable bundled alternative.

## Step 1: Probe Encoder Availability

Before any encoding work, check what encoders are available:

```bash
ffmpeg -encoders | grep -E "libx264|libopenh264|mpeg4"
```

Expected output shows which encoders are present:
- `libx264` - Preferred H.264 encoder (may be missing)
- `libopenh264` - Alternative H.264 (often has library issues)
- `mpeg4` - Universal fallback (always available)

## Step 2: Test Encoding on Single Short Clip

Never start batch processing without validation. Extract and test a short segment:

```bash
# Extract 5-second test clip
ffmpeg -y -i input.mp4 -ss 0 -t 5 -c copy test_clip.mp4

# Attempt encode with preferred codec
ffmpeg -y -i test_clip.mp4 -c:v libx264 -preset fast test_output.mp4
```

Check the exit code and output for errors. Common failures:
- `libopenh264.so: wrong ELF class`
- `Encoder libx264 not found`
- Library version mismatches

## Step 3: Progressive Fallback Strategy

If the preferred encoder fails, try these fallbacks in order:

### Fallback A: Copy Mode (No Re-encoding)
```bash
ffmpeg -y -i input.mp4 -c:v copy -c:a copy output.mp4
```
Fast, lossless, but doesn't change codec/format.

### Fallback B: MPEG4 Codec
```bash
ffmpeg -y -i input.mp4 -c:v mpeg4 -q:v 3 -c:a copy output.mp4
```
Universal compatibility, larger file sizes, always available.

### Fallback C: Install MoviePy (Bundles Working FFmpeg)
```bash
pip install moviepy
```

Then use Python instead of raw ffmpeg:
```python
from moviepy.editor import VideoFileClip, concatenate_videoclips

# Single clip processing
clip = VideoFileClip("input.mp4")
clip.write_videofile("output.mp4", codec="libx264")

# Concatenate multiple clips
clips = [VideoFileClip(f) for f in clip_files]
final = concatenate_videoclips(clips)
final.write_videofile("output.mp4", codec="libx264")
```

MoviePy bundles its own ffmpeg binary, avoiding system library issues.

## Step 4: Implementation Pattern

Here's a complete graceful degradation workflow:

```python
import subprocess
import os

def safe_video_encode(input_path, output_path, clips=None):
    """
    Encode video with graceful degradation fallbacks.
    
    Args:
        input_path: Single input file path, or
        clips: List of clip paths for concatenation
    
    Returns:
        True if successful, False otherwise
    """
    
    # Step 1: Check encoder availability
    result = subprocess.run(
        ["ffmpeg", "-encoders"],
        capture_output=True, text=True
    )
    has_libx264 = "libx264" in result.stdout
    has_mpeg4 = "mpeg4" in result.stdout
    
    # Step 2: If concatenating, prepare clips with moviepy
    if clips:
        try:
            from moviepy.editor import VideoFileClip, concatenate_videoclips
            loaded_clips = [VideoFileClip(c) for c in clips]
            final = concatenate_videoclips(loaded_clips)
            final.write_videofile(output_path, codec="libx264", logger=None)
            return True
        except Exception as e:
            print(f"MoviePy failed: {e}")
    
    # Step 3: Try ffmpeg with progressive fallbacks
    encoders_to_try = []
    if has_libx264:
        encoders_to_try.append("libx264")
    encoders_to_try.append("mpeg4")  # Always available
    
    for codec in encoders_to_try:
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-c:v", codec,
            "-c:a", "copy",
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True
    
    # Step 4: Last resort - copy mode
    cmd = ["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0
```

## Decision Flow

```
Start
  │
  ▼
Check encoders (ffmpeg -encoders)
  │
  ├─ libx264 available? ──Yes──► Try libx264
  │         │                       │
  │         No                      └─► Success? ──Yes──► Done
  │         │                                      │
  │         ▼                                      No
  │    Test short clip                             │
  │         │                                      ▼
  │         ▼                               Try -c:v copy
  │    Encode fails? ──Yes──► Check libopenh264    │
  │         │                       │              │
  │         No                      Broken         ▼
  │         │                       │         Try mpeg4
  │         ▼                       ▼              │
  │      Done                  Install moviepy     │
  │                              │                 │
  │                              ▼                 │
  │                         Use VideoFileClip ◄────┘
  │                         concatenate_videoclips
  │                              │
  │                              ▼
  │                            Done
  │
  ▼
End
```

## Key Principles

1. **Test first, batch later** - Always validate on a short clip
2. **Fail fast, fallback gracefully** - Don't waste time on doomed encodings
3. **MoviePy as safety net** - Its bundled ffmpeg avoids system issues
4. **Copy mode preserves content** - Even if quality isn't ideal

## Common Error Patterns

| Error Message | Cause | Solution |
|--------------|-------|----------|
| `libopenh264.so: wrong ELF class` | Library architecture mismatch | Use moviepy or mpeg4 |
| `Encoder libx264 not found` | FFmpeg built without x264 | Use mpeg4 fallback |
| `Broken pipe` | Process killed mid-operation | Try copy mode first |
| `Invalid data found` | Corrupt or incompatible input | Re-extract source |

## When to Use This Skill

- Processing user-uploaded videos (unknown codecs/formats)
- Running in containers with limited codec support
- Batch processing where failure would be costly
- Cross-platform deployments with varying ffmpeg builds