---
name: ffmpeg-encoder-check-4855c0
description: Check available FFmpeg encoders before writing encoding scripts to avoid library version mismatches
---

# FFmpeg Encoder Availability Check

## Purpose

Before writing any FFmpeg encoding script, always probe the system for available encoders. This prevents failures from missing or incompatible codec libraries (especially libopenh264 which frequently has version mismatches).

## Core Pattern

### Step 1: Probe Available Encoders

Always run this command before deciding on encoding parameters:

```bash
ffmpeg -encoders | grep h264
```

This shows which H.264 encoders are available on the system.

### Step 2: Choose Encoder Based on Availability

**Priority order for H.264 encoding:**

1. **`-c:v copy`** - If source and target resolution/format match, copy the stream without re-encoding (fastest, no quality loss)

2. **`-c:v libx264`** - If available, this is the most reliable and widely-compatible H.264 encoder

3. **`-c:v h264`** - Hardware acceleration if available (varies by system)

4. **Avoid `libopenh264`** - This encoder frequently has library version mismatches causing runtime failures

### Step 3: Verify Before Execution

After choosing an encoder, verify it works with a short test:

```bash
ffmpeg -t 5 -i input.mp4 -c:v libx264 -preset fast -crf 23 -c:a copy test_output.mp4
```

## Example Decision Flow

```bash
#!/bin/bash

# Check available encoders
ENCODERS=$(ffmpeg -encoders 2>/dev/null | grep h264)

if echo "$ENCODERS" | grep -q "libx264"; then
    VIDEO_CODEC="libx264"
    echo "Using libx264 encoder"
elif echo "$ENCODERS" | grep -q "h264"; then
    VIDEO_CODEC="h264"
    echo "Using h264 encoder"
else
    VIDEO_CODEC="copy"
    echo "No H.264 encoder available, using stream copy"
fi

# Use $VIDEO_CODEC in your ffmpeg command
ffmpeg -i input.mp4 -c:v $VIDEO_CODEC output.mp4
```

## When to Use Stream Copy

Use `-c:v copy` when:
- Source and target resolution are identical
- Source codec is already H.264
- You only need to change container format or audio
- Speed is critical and re-encoding is unnecessary

## Common Pitfalls

- **Don't assume encoder availability** - Different systems have different FFmpeg builds
- **Don't hardcode libopenh264** - High failure rate due to library mismatches
- **Always test encoding** - Run a short segment test before processing full files
- **Check both video and audio codecs** - Audio encoder availability matters too