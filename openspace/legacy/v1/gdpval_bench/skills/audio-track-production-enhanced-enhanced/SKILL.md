---
name: aligned-stem-workflow
description: Incremental audio production with duration alignment handling, per-stem verification, and adaptive extension strategies
---

# Aligned Stem Audio Production Workflow

This skill provides a resilient pattern for audio production that emphasizes **incremental verification**, **fail-fast** principles, and **automatic duration alignment**. Each major step produces verified outputs before proceeding, with explicit handling for stem duration mismatches using appropriate extension strategies.

## Overview

Follow these steps in strict order. Each step must complete successfully and pass verification before proceeding to the next:

1. **Early timing calculation** - Derive section transitions from BPM and duration first
2. **Verify reference audio** - Validate input file properties and extract target duration
3. **Generate and verify each stem individually** - One stem at a time with immediate verification
4. **Detect and resolve duration mismatches** - Apply appropriate extension strategy (zero-pad, loop, or crossfade)
5. **Generate drum stem separately** - Dedicated drum extension with rhythm patterns
6. **Apply effects with verification** - Process each stem and verify output
7. **Export master track** - Mix all verified stems
8. **Archive and final verification** - Package deliverables with comprehensive checks

## Key Differences from Standard Workflow

- **Incremental verification**: Verify each stem immediately after generation, not just at the end
- **Fail-fast approach**: Stop and report errors at each step rather than accumulating failures
- **Early timing**: Calculate section transitions before any audio generation
- **Duration alignment**: Explicit detection and resolution of stem duration mismatches
- **Adaptive extension**: Choose appropriate strategy (zero-pad/loop/crossfade) based on stem type
- **Separated drums**: Drum stem generation is a distinct step with rhythm-specific processing
- **Memory-efficient**: Process stems individually to avoid large array operations that cause sandbox failures

## Step 1: Calculate Timing Parameters (Early)

Calculate all timing parameters **before** generating any audio. This ensures consistent timing across all stems:

```python
def calculate_section_transitions(bpm, total_duration_sec, sections):
    """Calculate beat-aligned transition points for song sections."""
    beats_per_second = bpm / 60.0
    
    section_durations = {}
    cumulative_time = 0
    
    for section_name, beat_count in sections.items():
        duration = beat_count / beats_per_second
        section_durations[section_name] = {
            'start': cumulative_time,
            'end': cumulative_time + duration,
            'beats': beat_count,
            'start_beat': cumulative_time * beats_per_second
        }
        cumulative_time += duration
    
    return section_durations

# Configuration
BPM = 120
DURATION = 137
SECTIONS = {'intro': 16, 'verse': 32, 'chorus': 32, 'bridge': 16, 'outro': 16}

timing = calculate_section_transitions(BPM, DURATION, SECTIONS)
print("Timing calculated:")
for section, data in timing.items():
    print(f"  {section}: {data['start']:.2f}s - {data['end']:.2f}s ({data['beats']} beats)")
```

## Step 2: Verify Reference Audio

Validate the reference file exists and has expected properties:

```python
import soundfile as sf
import os

def verify_reference_file(filepath, expected_sample_rate=None, min_duration=None):
    """Verify reference audio file and return info dict."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Reference file not found: {filepath}")
    
    info = sf.info(filepath)
    errors = []
    
    if expected_sample_rate and info.samplerate != expected_sample_rate:
        errors.append(f"Sample rate mismatch: expected {expected_sample_rate}, got {info.samplerate}")
    
    if min_duration and info.duration < min_duration:
        errors.append(f"Duration too short: expected >= {min_duration}s, got {info.duration}s")
    
    if errors:
        raise ValueError(f"Reference file validation failed: {'; '.join(errors)}")
    
    print(f"Reference verified: {info.duration:.2f}s @ {info.samplerate}Hz, {info.channels}ch, {info.subtype}")
    return {
        'sample_rate': info.samplerate,
        'duration': info.duration,
        'channels': info.channels,
        'subtype': info.subtype
    }

# Verify reference
ref_info = verify_reference_file('reference.wav', expected_sample_rate=48000, min_duration=130)
TARGET_DURATION = ref_info['duration']  # Use actual reference duration as target
```

## Step 3: Generate and Verify Each Stem Individually

Generate one stem at a time, verify it immediately before proceeding to the next:

```python
import numpy as np

def generate_stem(name, duration_sec, sample_rate, subtype='FLOAT', section_timing=None):
    """Generate a single stem with explicit sample type."""
    frames = int(duration_sec * sample_rate)
    t = np.linspace(0, duration_sec, frames)
    
    # Generate stem-specific content (customize per stem type)
    if name == 'bass':
        freq = 110  # A2
        audio_data = np.sin(2 * np.pi * freq * t) * 0.8
    elif name == 'guitars':
        freq = 440  # A4
        audio_data = np.sin(2 * np.pi * freq * t) * 0.6
    elif name == 'synths':
        freq = 880  # A5
        audio_data = np.sin(2 * np.pi * freq * t) * 0.5
    elif name == 'bridge':
        freq = 220  # A3
        audio_data = np.sin(2 * np.pi * freq * t) * 0.7
    else:
        audio_data = np.sin(2 * np.pi * 440 * t) * 0.5
    
    # Ensure proper data type
    if subtype == 'FLOAT':
        audio_data = audio_data.astype(np.float32)
    elif subtype == 'PCM_24':
        audio_data = np.clip(audio_data, -1, 1) * (2**23 - 1)
        audio_data = audio_data.astype(np.int32)
    
    filepath = f'{name}_stem.wav'
    sf.write(filepath, audio_data, sample_rate, subtype=subtype, format='WAV')
    
    return filepath, audio_data

def verify_stem(filepath, expected_sample_rate, expected_subtype, expected_duration, tolerance_sec=1.0):
    """Verify a single stem meets specifications."""
    if not os.path.exists(filepath):
        return {'success': False, 'error': f'File not found: {filepath}'}
    
    info = sf.info(filepath)
    errors = []
    
    if info.samplerate != expected_sample_rate:
        errors.append(f'sample_rate: expected {expected_sample_rate}, got {info.samplerate}')
    
    if info.subtype != expected_subtype:
        errors.append(f'subtype: expected {expected_subtype}, got {info.subtype}')
    
    if abs(info.duration - expected_duration) > tolerance_sec:
        errors.append(f'duration: expected ~{expected_duration}s, got {info.duration}s')
    
    # Calculate duration discrepancy
    duration_diff = info.duration - expected_duration
    
    if errors:
        return {'success': False, 'error': '; '.join(errors), 'duration_diff': duration_diff}
    
    return {'success': True, 'info': info, 'duration_diff': duration_diff}

# Generate stems one at a time with verification
SAMPLE_RATE = 48000
SUBTYPE = 'FLOAT'
STEM_NAMES = ['bass', 'guitars', 'synths', 'bridge']

generated_stems = []
stem_info = {}  # Track duration discrepancies

for stem_name in STEM_NAMES:
    print(f"\n=== Generating {stem_name} stem ===")
    
    # Generate
    filepath, data = generate_stem(stem_name, DURATION, SAMPLE_RATE, subtype=SUBTYPE)
    
    # Verify immediately
    result = verify_stem(filepath, SAMPLE_RATE, SUBTYPE, TARGET_DURATION)
    
    if result['success']:
        print(f"✓ {stem_name} stem verified: {result['info'].duration:.2f}s @ {result['info'].samplerate}Hz")
        if abs(result['duration_diff']) > 0.1:
            print(f"  ⚠ Duration discrepancy: {result['duration_diff']:+.2f}s")
        generated_stems.append(filepath)
        stem_info[stem_name] = result
    else:
        print(f"✗ {stem_name} stem FAILED: {result['error']}")
        raise RuntimeError(f"Stem generation failed for {stem_name}: {result['error']}")

print(f"\nAll {len(generated_stems)} stems generated and verified successfully")
```

## Step 4: Detect and Resolve Duration Mismatches

When stems have different durations, apply the appropriate extension strategy:

### Strategy Selection Guidelines

| Strategy | Best For | Duration Gap | Sound Characteristic |
|----------|----------|--------------|---------------------|
| **Zero-padding** | Short gaps (<0.5s), silence sections, endings | Small | Clean, abrupt |
| **Looping** | Repetitive patterns (drums, bass, rhythmic elements) | Medium to large | Seamless, rhythmic |
| **Crossfade extension** | Melodic content, sustained instruments, vocals | Any | Natural, smooth |

```python
def align_stem_duration(input_filepath, output_filepath, target_duration, strategy='auto', 
                        sample_rate=None, subtype='FLOAT', loop_seamless=True):
    """
    Align stem duration to target using appropriate strategy.
    
    Args:
        input_filepath: Path to source stem
        output_filepath: Path for aligned output
        target_duration: Target duration in seconds
        strategy: 'zero_pad', 'loop', 'crossfade', or 'auto'
        sample_rate: Sample rate (auto-detected if None)
        subtype: Audio subtype
        loop_seamless: Apply crossfade at loop boundaries if True
    
    Returns:
        dict with success status and alignment details
    """
    if not os.path.exists(input_filepath):
        return {'success': False, 'error': f'Input file not found: {input_filepath}'}
    
    # Load source
    data, sr = sf.read(input_filepath)
    if sample_rate is None:
        sample_rate = sr
    
    source_duration = len(data) / sample_rate
    duration_diff = target_duration - source_duration
    
    # If already aligned (within tolerance), just copy
    if abs(duration_diff) < 0.01:
        sf.write(output_filepath, data, sample_rate, subtype=subtype, format='WAV')
        return {'success': True, 'strategy': 'none', 'duration_diff': 0}
    
    if duration_diff > 0:
        # Need to EXTEND
        extend_frames = int(duration_diff * sample_rate)
        
        if strategy == 'auto':
            # Auto-select based on duration gap and stem type
            if duration_diff < 0.5:
                strategy = 'zero_pad'
            elif 'drum' in input_filepath or 'bass' in input_filepath:
                strategy = 'loop'
            else:
                strategy = 'crossfade'
        
        if strategy == 'zero_pad':
            # Append zeros
            padding = np.zeros(extend_frames, dtype=data.dtype)
            aligned_data = np.concatenate([data, padding])
            
        elif strategy == 'loop':
            # Loop the content
            loop_frames = len(data)
            loops_needed = int(np.ceil(extend_frames / loop_frames))
            
            if loop_seamless and loops_needed > 1:
                # Apply crossfade at loop boundaries for seamless looping
                crossfade_frames = min(int(0.05 * sample_rate), loop_frames // 4)
                loop_extension = np.zeros(extend_frames, dtype=data.dtype)
                
                for i in range(loops_needed):
                    start = i * loop_frames
                    end = min(start + loop_frames, extend_frames)
                    actual_len = end - start
                    
                    # Extract loop segment
                    loop_segment = data[:actual_len].copy()
                    
                    # Apply crossfade at boundaries
                    if i > 0 and actual_len >= crossfade_frames * 2:
                        # Fade in from previous loop
                        fade_in = np.linspace(0, 1, crossfade_frames)
                        loop_segment[:crossfade_frames] *= fade_in
                    
                    if i < loops_needed - 1 and actual_len >= crossfade_frames * 2:
                        # Fade out for next loop
                        fade_out = np.linspace(1, 0, crossfade_frames)
                        loop_segment[-crossfade_frames:] *= fade_out
                    
                    loop_extension[start:end] = loop_segment
                
                extend_frames_actual = len(loop_extension)
            else:
                # Simple tiling
                loop_extension = np.tile(data, loops_needed)[:extend_frames]
                extend_frames_actual = extend_frames
            
            aligned_data = np.concatenate([data, loop_extension[:extend_frames_actual]])
            
        elif strategy == 'crossfade':
            # Extend using crossfade from the end of the source
            # Take last portion and crossfade it onto itself
            fade_duration = min(duration_diff * 0.3, 2.0)  # 30% of gap, max 2s
            fade_frames = int(fade_duration * sample_rate)
            
            if fade_frames >= len(data) // 2:
                # Source too short for crossfade, fall back to loop
                fade_frames = len(data) // 4
            
            # Extract tail segment for extension
            tail_segment = data[-fade_frames:].copy()
            
            # Create extended portion with crossfade
            extended_portion = np.zeros(extend_frames, dtype=data.dtype)
            
            if extend_frames <= fade_frames:
                # Short extension: just crossfade tail onto itself
                fade_in = np.linspace(0, 1, extend_frames)
                extended_portion = tail_segment[:extend_frames] * fade_in
            else:
                # Longer extension: loop tail with crossfades
                loops = int(np.ceil(extend_frames / fade_frames))
                for i in range(loops):
                    start = i * fade_frames
                    end = min(start + fade_frames, extend_frames)
                    seg_len = end - start
                    
                    segment = tail_segment[:seg_len].copy()
                    
                    # Crossfade boundaries
                    if seg_len >= 100:
                        cf_len = min(50, seg_len // 4)
                        if i > 0:
                            fade_in = np.linspace(0, 1, cf_len)
                            segment[:cf_len] *= fade_in
                    
                    extended_portion[start:end] = segment
            
            aligned_data = np.concatenate([data, extended_portion])
        
        else:
            return {'success': False, 'error': f'Unknown extension strategy: {strategy}'}
        
    else:
        # Need to TRUNCATE
        truncate_frames = int(abs(duration_diff) * sample_rate)
        aligned_data = data[:len(data) - truncate_frames]
        strategy = 'truncate'
    
    # Ensure proper data type and clip
    if subtype == 'FLOAT':
        aligned_data = aligned_data.astype(np.float32)
    elif subtype == 'PCM_24':
        aligned_data = np.clip(aligned_data, -1, 1) * (2**23 - 1)
        aligned_data = aligned_data.astype(np.int32)
    else:
        aligned_data = np.clip(aligned_data, -1, 1)
    
    # Export aligned stem
    sf.write(output_filepath, aligned_data, sample_rate, subtype=subtype, format='WAV')
    
    return {
        'success': True, 
        'strategy': strategy,
        'source_duration': source_duration,
        'target_duration': target_duration,
        'duration_diff': duration_diff,
        'aligned_frames': len(aligned_data)
    }

# Apply duration alignment to all stems
print("\n=== Aligning stem durations ===")
aligned_stems = []

TARGET_DURATION = ref_info['duration']  # Use reference as target

for stem_name in STEM_NAMES:
    input_file = f'{stem_name}_stem.wav'
    output_file = f'{stem_name}_aligned.wav'
    
    # Determine strategy based on stem type
    if stem_name in ['bass', 'drums']:
        strategy = 'loop'  # Rhythmic elements loop well
    elif stem_name in ['bridge', 'outro']:
        strategy = 'crossfade'  # Sustained content benefits from crossfade
    else:
        strategy = 'auto'  # Let the function decide
    
    print(f"Aligning {stem_name} (strategy: {strategy})...")
    result = align_stem_duration(input_file, output_file, TARGET_DURATION, 
                                  strategy=strategy, sample_rate=SAMPLE_RATE, subtype=SUBTYPE)
    
    if result['success']:
        if result['strategy'] != 'none':
            print(f"✓ {stem_name} aligned: {result['source_duration']:.2f}s -> {result['target_duration']:.2f}s via {result['strategy']}")
        else:
            print(f"✓ {stem_name} already aligned at {result['target_duration']:.2f}s")
        aligned_stems.append(output_file)
    else:
        print(f"✗ {stem_name} alignment FAILED: {result['error']}")
        raise RuntimeError(f"Duration alignment failed for {stem_name}: {result['error']}")

print(f"\nAll {len(aligned_stems)} stems duration-aligned successfully")
```

## Step 5: Generate Drum Stem Separately

Drums require different processing (rhythm patterns, percussion sounds):

```python
def generate_drum_stem(duration_sec, sample_rate, bpm, section_timing, subtype='FLOAT'):
    """Generate drum stem with rhythm patterns aligned to sections."""
    frames = int(duration_sec * sample_rate)
    audio_data = np.zeros(frames, dtype=np.float32)
    beats_per_second = bpm / 60.0
    
    # Simple kick drum pattern (every beat)
    kick_freq = 60
    kick_duration = 0.1
    kick_frames = int(kick_duration * sample_rate)
    
    for beat_time in np.arange(0, duration_sec, 1.0 / beats_per_second):
        start_frame = int(beat_time * sample_rate)
        end_frame = min(start_frame + kick_frames, frames)
        
        if start_frame < frames:
            t = np.linspace(0, kick_duration, end_frame - start_frame)
            kick = np.exp(-5 * t) * np.sin(2 * np.pi * kick_freq * t)
            audio_data[start_frame:end_frame] += kick * 0.9
    
    # Simple snare pattern (every 2nd and 4th beat)
    snare_freq = 200
    snare_duration = 0.05
    snare_frames = int(snare_duration * sample_rate)
    
    for beat_time in np.arange(0, duration_sec, 2.0 / beats_per_second):
        start_frame = int((beat_time + 0.5 / beats_per_second) * sample_rate)
        end_frame = min(start_frame + snare_frames, frames)
        
        if start_frame < frames:
            t = np.linspace(0, snare_duration, end_frame - start_frame)
            snare = np.exp(-10 * t) * np.random.uniform(-1, 1, len(t)) * 0.5
            audio_data[start_frame:end_frame] += snare * 0.7
    
    audio_data = np.clip(audio_data, -1, 1)
    
    filepath = 'drums_stem.wav'
    sf.write(filepath, audio_data, sample_rate, subtype=subtype, format='WAV')
    
    return filepath, audio_data

print("\n=== Generating drum stem ===")
drums_filepath, drums_data = generate_drum_stem(TARGET_DURATION, SAMPLE_RATE, BPM, timing, subtype=SUBTYPE)

drums_result = verify_stem(drums_filepath, SAMPLE_RATE, SUBTYPE, TARGET_DURATION)
if drums_result['success']:
    print(f"✓ Drum stem verified: {drums_result['info'].duration:.2f}s @ {drums_result['info'].samplerate}Hz")
    # Align drums if needed (usually uses loop strategy)
    drums_aligned = 'drums_aligned.wav'
    drums_align_result = align_stem_duration(drums_filepath, drums_aligned, TARGET_DURATION,
                                              strategy='loop', sample_rate=SAMPLE_RATE, subtype=SUBTYPE)
    if drums_align_result['success']:
        aligned_stems.append(drums_aligned)
        print(f"✓ Drums aligned via {drums_align_result['strategy']}")
    else:
        print(f"✗ Drum alignment FAILED: {drums_align_result['error']}")
        raise RuntimeError(f"Drum alignment failed: {drums_align_result['error']}")
else:
    print(f"✗ Drum stem FAILED: {drums_result['error']}")
    raise RuntimeError(f"Drum stem generation failed: {drums_result['error']}")
```

## Step 6: Apply Effects with Verification

Process each aligned stem and verify the output:

```python
from scipy import signal

def apply_lowpass_filter(audio_data, sample_rate, cutoff_freq=8000):
    """Apply lowpass filter using scipy.signal."""
    nyquist = sample_rate / 2
    normalized_cutoff = cutoff_freq / nyquist
    b, a = signal.butter(4, normalized_cutoff, btype='low')
    return signal.filtfilt(b, a, audio_data)

def apply_effects_and_verify(input_filepath, output_filepath, sample_rate, subtype):
    """Apply effects to stem and verify output."""
    data, sr = sf.read(input_filepath)
    
    # Apply effects
    processed = apply_lowpass_filter(data, sr, cutoff_freq=8000)
    processed = np.clip(processed, -1, 1)
    
    # Export
    sf.write(output_filepath, processed, sample_rate, subtype=subtype, format='WAV')
    
    # Verify
    result = verify_stem(output_filepath, sample_rate, subtype, TARGET_DURATION, tolerance_sec=2.0)
    return result, processed

print("\n=== Applying effects to all aligned stems ===")
processed_stems = []

for aligned_file in aligned_stems:
    stem_name = os.path.basename(aligned_file).replace('_aligned.wav', '')
    output_file = f'{stem_name}_processed.wav'
    
    print(f"Processing {stem_name}...")
    result, _ = apply_effects_and_verify(aligned_file, output_file, SAMPLE_RATE, SUBTYPE)
    
    if result['success']:
        print(f"✓ {stem_name} processed and verified")
        processed_stems.append(output_file)
    else:
        print(f"✗ {stem_name} processing FAILED: {result['error']}")
        raise RuntimeError(f"Effects processing failed for {stem_name}")

print(f"\nAll {len(processed_stems)} stems processed successfully")
```

## Step 7: Export Master Track

Mix all verified stems into master track:

```python
def create_master_track(stem_files, output_filepath, sample_rate, subtype):
    """Create master track from verified stems."""
    # Load first stem to get dimensions
    first_data, sr = sf.read(stem_files[0])
    master_audio = np.zeros(len(first_data), dtype=np.float32)
    
    # Mix all stems with gain staging
    gain_per_stem = 0.4  # Prevent clipping with 5 stems
    
    for i, stem_file in enumerate(stem_files):
        print(f"  Mixing stem {i+1}/{len(stem_files)}: {stem_file}")
        data, sr = sf.read(stem_file)
        
        # Ensure same length as master
        if len(data) > len(master_audio):
            data = data[:len(master_audio)]
        elif len(data) < len(master_audio):
            # Pad with zeros if shorter (shouldn't happen after alignment)
            padding = np.zeros(len(master_audio) - len(data), dtype=np.float32)
            data = np.concatenate([data, padding])
        
        master_audio += data * gain_per_stem
    
    # Apply master bus processing
    master_audio = np.clip(master_audio, -1, 1)
    
    # Export master
    sf.write(output_filepath, master_audio, sample_rate, subtype=subtype, format='WAV')
    
    return output_filepath, master_audio

print("\n=== Creating master track ===")
master_filepath, master_data = create_master_track(processed_stems, 'master.wav', SAMPLE_RATE, SUBTYPE)

# Verify master
master_info = sf.info(master_filepath)
print(f"Master exported: {master_filepath}")
print(f"  Duration: {master_info.duration:.2f}s")
print(f"  Sample rate: {master_info.samplerate}Hz")
print(f"  Channels: {master_info.channels}")
print(f"  Subtype: {master_info.subtype}")
```

## Step 8: Archive and Final Verification

Package deliverables with comprehensive checks:

```python
def create_archive(stem_files, master_file, output_archive='audio交付.zip'):
    """Create archive of all deliverables."""
    import zipfile
    
    all_files = stem_files + [master_file]
    
    with zipfile.ZipFile(output_archive, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for filepath in all_files:
            if os.path.exists(filepath):
                zipf.write(filepath)
                print(f"  Added: {filepath}")
    
    return output_archive

def final_verification(master_filepath, expected_duration=None, expected_sample_rate=None):
    """Comprehensive final verification."""
    issues = []
    
    # Verify master file
    if not os.path.exists(master_filepath):
        return {'success': False, 'error': 'Master file not found'}
    
    info = sf.info(master_filepath)
    
    if expected_duration and abs(info.duration - expected_duration) > 2.0:
        issues.append(f"Duration mismatch: expected ~{expected_duration}s, got {info.duration}s")
    
    if expected_sample_rate and info.samplerate != expected_sample_rate:
        issues.append(f"Sample rate mismatch: expected {expected_sample_rate}, got {info.samplerate}")
    
    # Check for clipping
    data, _ = sf.read(master_filepath)
    clip_ratio = np.sum(np.abs(data) >= 0.99) / len(data)
    if clip_ratio > 0.001:  # More than 0.1% clipped
        issues.append(f"Excessive clipping detected: {clip_ratio*100:.2f}% of samples at max level")
    
    # Check for silence
    rms = np.sqrt(np.mean(data**2))
    if rms < 0.01:
        issues.append(f"Audio too quiet: RMS level {rms:.4f}")
    
    success = len(issues) == 0
    
    return {
        'success': success,
        'issues': issues,
        'info': {
            'duration': info.duration,
            'sample_rate': info.samplerate,
            'channels': info.channels,
            'subtype': info.subtype,
            'clipping_ratio': clip_ratio,
            'rms_level': rms
        }
    }

print("\n=== Final verification ===")
final_result = final_verification(master_filepath, expected_duration=TARGET_DURATION, 
                                   expected_sample_rate=SAMPLE_RATE)

if final_result['success']:
    print("✓ All verification checks passed")
    print(f"  Master: {final_result['info']['duration']:.2f}s @ {final_result['info']['sample_rate']}Hz")
    print(f"  RMS level: {final_result['info']['rms_level']:.4f}")
    print(f"  Clipping: {final_result['info']['clipping_ratio']*100:.2f}%")
    
    # Create archive
    print("\n=== Creating archive ===")
    archive_path = create_archive(processed_stems, master_filepath, 'audio_deliverables.zip')
    print(f"✓ Archive created: {archive_path}")
else:
    print("✗ Verification failed:")
    for issue in final_result['issues']:
        print(f"  - {issue}")
```

## Troubleshooting

### Duration Mismatch Issues

**Problem**: Stems have significantly different durations causing alignment artifacts

**Solutions**:
1. **Check source generation**: Ensure all stems use the same duration parameter
2. **Adjust strategy**: Switch from `auto` to explicit strategy based on content type
3. **Tune crossfade parameters**: Increase `fade_duration` for smoother transitions
4. **Enable seamless looping**: Set `loop_seamless=True` for rhythmic content

### Extension Quality Issues

**Problem**: Loop points audible or crossfade sounds unnatural

**Solutions**:
1. **For looping**: Find better loop points (at zero-crossings or beat boundaries)
2. **For crossfade**: Increase overlap duration or use different source segments
3. **For padding**: Only use for very short gaps (<0.3s) at song endings

### Memory Issues

**Problem**: Large files cause sandbox failures

**Solutions**:
1. **Process in chunks**: Use streaming I/O for very long files
2. **Reduce sample rate**: Temporarily work at 44.1kHz, upsample for final export
3. **Process stems sequentially**: Clear memory between stem operations
