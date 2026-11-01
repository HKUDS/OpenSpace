---
name: adaptive-stem-alignment
description: Incremental audio production with duration mismatch handling, adaptive stem extension, and pre-mix alignment verification
---

# Adaptive Stem Alignment Workflow

This skill provides a resilient pattern for audio production that emphasizes **incremental verification**, **fail-fast** principles, and **adaptive duration handling**. Each major step produces verified outputs before proceeding, with explicit strategies for handling stems of different durations.

## Overview

Follow these steps in strict order. Each step must complete successfully and pass verification before proceeding to the next:

1. **Early timing calculation** - Derive section transitions from BPM and duration first
2. **Verify reference audio** - Validate input file properties and establish target duration
3. **Generate and verify each stem individually** - One stem at a time with immediate verification
4. **Generate drum stem separately** - Dedicated drum extension with rhythm patterns
5. **Align stem durations** - Handle duration mismatches with adaptive extension strategies
6. **Apply effects with verification** - Process each stem and verify output
7. **Export master track** - Mix all verified stems
8. **Archive and final verification** - Package deliverables with comprehensive checks

## Key Differences from Standard Workflow

- **Incremental verification**: Verify each stem immediately after generation, not just at the end
- **Fail-fast approach**: Stop and report errors at each step rather than accumulating failures
- **Early timing**: Calculate section transitions before any audio generation
- **Separated drums**: Drum stem generation is a distinct step with rhythm-specific processing
- **Memory-efficient**: Process stems individually to avoid large array operations that cause sandbox failures
- **Adaptive duration handling**: Explicit strategies for mismatched stem durations (zero-padding, looping, crossfade extension)
- **Pre-mix alignment**: Verify all stems match target duration before mixing

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
TARGET_DURATION = ref_info['duration']  # Use reference duration as target
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

def verify_stem(filepath, expected_sample_rate, expected_subtype, expected_duration):
    """Verify a single stem meets specifications."""
    if not os.path.exists(filepath):
        return {'success': False, 'error': f'File not found: {filepath}'}
    
    info = sf.info(filepath)
    errors = []
    
    if info.samplerate != expected_sample_rate:
        errors.append(f'sample_rate: expected {expected_sample_rate}, got {info.samplerate}')
    
    if info.subtype != expected_subtype:
        errors.append(f'subtype: expected {expected_subtype}, got {info.subtype}')
    
    if abs(info.duration - expected_duration) > 1.0:  # Allow 1s tolerance
        errors.append(f'duration: expected ~{expected_duration}s, got {info.duration}s')
    
    if errors:
        return {'success': False, 'error': '; '.join(errors)}
    
    return {'success': True, 'info': info}

# Generate stems one at a time with verification
SAMPLE_RATE = 48000
SUBTYPE = 'FLOAT'
STEM_NAMES = ['bass', 'guitars', 'synths', 'bridge']

generated_stems = []
stem_durations = {}  # Track actual durations for alignment step

for stem_name in STEM_NAMES:
    print(f"\n=== Generating {stem_name} stem ===")
    
    # Generate
    filepath, data = generate_stem(stem_name, DURATION, SAMPLE_RATE, subtype=SUBTYPE)
    
    # Verify immediately
    result = verify_stem(filepath, SAMPLE_RATE, SUBTYPE, DURATION)
    
    if result['success']:
        print(f"✓ {stem_name} stem verified: {result['info'].duration:.2f}s @ {result['info'].samplerate}Hz")
        generated_stems.append(filepath)
        stem_durations[stem_name] = result['info'].duration
    else:
        print(f"✗ {stem_name} stem FAILED: {result['error']}")
        raise RuntimeError(f"Stem generation failed for {stem_name}: {result['error']}")

print(f"\nAll {len(generated_stems)} stems generated and verified successfully")
```

## Step 4: Generate Drum Stem Separately

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
drums_filepath, drums_data = generate_drum_stem(DURATION, SAMPLE_RATE, BPM, timing, subtype=SUBTYPE)

drums_result = verify_stem(drums_filepath, SAMPLE_RATE, SUBTYPE, DURATION)
if drums_result['success']:
    print(f"✓ Drum stem verified: {drums_result['info'].duration:.2f}s @ {drums_result['info'].samplerate}Hz")
    generated_stems.append(drums_filepath)
    stem_durations['drums'] = drums_result['info'].duration
else:
    print(f"✗ Drum stem FAILED: {drums_result['error']}")
    raise RuntimeError(f"Drum stem generation failed: {drums_result['error']}")
```

## Step 5: Align Stem Durations (NEW)

Handle duration mismatches with adaptive extension strategies. Choose the appropriate method based on stem type:

### Duration Mismatch Handling Strategies

| Strategy | Best For | How It Works | Considerations |
|----------|----------|--------------|----------------|
| **Zero-padding** | Ambient pads, drones, FX | Append silence to match target duration | Simple, no artifacts, but may create abrupt endings |
| **Looping** | Rhythmic elements, drums, percussion | Repeat content to fill duration | Maintains rhythm, but requires beat-aligned loop points |
| **Crossfade extension** | Melodic elements, vocals, guitars | Fade out original, crossfade with looped/faded content | Smoothest transition, but requires careful fade curve design |
| **Time-stretch** | Any content (when quality matters) | Use phase vocoder to stretch without pitch shift | Computationally expensive, may introduce artifacts |

```python
def align_stem_duration(input_filepath, output_filepath, target_duration, sample_rate, 
                        subtype='FLOAT', strategy='auto', stem_type=None):
    """
    Align stem duration to target using appropriate strategy.
    
    Args:
        input_filepath: Path to input stem
        output_filepath: Path for aligned output
        target_duration: Target duration in seconds
        sample_rate: Sample rate
        subtype: Audio subtype (FLOAT, PCM_24, etc.)
        strategy: 'zero_pad', 'loop', 'crossfade', 'auto'
        stem_type: Type of stem ('rhythmic', 'melodic', 'ambient', 'percussion')
    
    Returns:
        dict with success status and alignment info
    """
    if not os.path.exists(input_filepath):
        return {'success': False, 'error': f'Input file not found: {input_filepath}'}
    
    # Load audio
    data, sr = sf.read(input_filepath)
    current_duration = len(data) / sr
    
    # Check if alignment needed (allow 0.5s tolerance)
    if abs(current_duration - target_duration) < 0.5:
        print(f"  Duration already aligned: {current_duration:.2f}s ≈ {target_duration:.2f}s")
        # Just copy file
        sf.write(output_filepath, data, sample_rate, subtype=subtype, format='WAV')
        return {'success': True, 'strategy': 'none', 'original_duration': current_duration}
    
    # Auto-detect strategy if not specified
    if strategy == 'auto':
        if stem_type in ['rhythmic', 'percussion', 'drums']:
            strategy = 'loop'
        elif stem_type in ['ambient', 'pad', 'drone', 'fx']:
            strategy = 'zero_pad'
        else:  # melodic, vocals, guitars, etc.
            strategy = 'crossfade'
    
    print(f"  Aligning duration: {current_duration:.2f}s → {target_duration:.2f}s using '{strategy}'")
    
    # Calculate frames needed
    target_frames = int(target_duration * sample_rate)
    current_frames = len(data)
    
    if strategy == 'zero_pad':
        # Simple zero-padding
        if current_frames < target_frames:
            aligned_data = np.zeros(target_frames, dtype=data.dtype)
            aligned_data[:current_frames] = data
        else:
            # Truncate with fade-out
            fade_frames = int(0.5 * sample_rate)  # 500ms fade
            aligned_data = data[:target_frames].copy()
            if target_frames < current_frames:
                fade_start = max(0, target_frames - fade_frames)
                fade_curve = np.linspace(1, 0, target_frames - fade_start)
                aligned_data[fade_start:] *= fade_curve
    
    elif strategy == 'loop':
        # Loop to fill duration
        aligned_data = np.zeros(target_frames, dtype=data.dtype)
        loop_count = (target_frames // current_frames) + 1
        
        for i in range(loop_count):
            start = i * current_frames
            end = min(start + current_frames, target_frames)
            copy_len = end - start
            
            if copy_len > 0:
                aligned_data[start:end] = data[:copy_len]
        
        # Apply crossfade at loop points to avoid clicks
        crossfade_frames = int(0.05 * sample_rate)  # 50ms crossfade
        if current_frames > crossfade_frames * 2:
            for i in range(1, loop_count):
                loop_start = i * current_frames
                if loop_start < target_frames:
                    # Crossfade region
                    cf_end = min(loop_start + crossfade_frames, target_frames)
                    cf_start = max(loop_start - crossfade_frames, 0)
                    if cf_end > cf_start:
                        fade_in = np.linspace(0, 1, cf_end - cf_start)
                        fade_out = np.linspace(1, 0, cf_end - cf_start)
                        aligned_data[cf_start:cf_end] = (
                            aligned_data[cf_start:cf_end] * fade_out +
                            np.roll(aligned_data[cf_start:cf_end], -current_frames) * fade_in
                        )
    
    elif strategy == 'crossfade':
        # Crossfade-based extension with smooth transition
        if current_frames < target_frames:
            # Extend with crossfaded loop
            extension_frames = target_frames - current_frames
            fade_frames = min(int(2.0 * sample_rate), extension_frames // 2)  # Max 2s fade
            
            # Create extension from looped content with fade
            extension_data = np.zeros(extension_frames, dtype=data.dtype)
            
            if extension_frames <= current_frames:
                # Just take from beginning with fade-in
                extension_data[:extension_frames] = data[:extension_frames]
                if fade_frames > 0:
                    fade_in = np.linspace(0, 1, min(fade_frames, extension_frames))
                    extension_data[:len(fade_in)] *= fade_in
            else:
                # Loop multiple times with crossfades
                loop_data = np.tile(data, (extension_frames // current_frames) + 2)[:extension_frames]
                
                # Apply fade-in to extension
                if fade_frames > 0:
                    fade_in = np.linspace(0, 1, fade_frames)
                    loop_data[:fade_frames] *= fade_in
                
                extension_data = loop_data
            
            # Concatenate with crossfade
            aligned_data = np.zeros(target_frames, dtype=data.dtype)
            aligned_data[:current_frames] = data
            
            # Crossfade region at junction
            if fade_frames > 0:
                junction_start = current_frames - fade_frames
                junction_end = min(current_frames + fade_frames, target_frames)
                
                if junction_end > junction_start:
                    crossfade_len = junction_end - junction_start
                    fade_out = np.linspace(1, 0, crossfade_len)
                    fade_in = np.linspace(0, 1, crossfade_len)
                    
                    aligned_data[junction_start:junction_end] = (
                        aligned_data[junction_start:junction_end] * fade_out +
                        extension_data[:crossfade_len] * fade_in
                    )
                else:
                    aligned_data[current_frames:current_frames + extension_frames] = extension_data
            else:
                aligned_data[current_frames:] = extension_data
        else:
            # Truncate with fade-out
            fade_frames = int(2.0 * sample_rate)
            aligned_data = data[:target_frames].copy()
            fade_start = max(0, target_frames - fade_frames)
            fade_curve = np.linspace(1, 0, target_frames - fade_start)
            aligned_data[fade_start:] *= fade_curve
    
    else:
        return {'success': False, 'error': f'Unknown strategy: {strategy}'}
    
    # Clip to prevent overload
    aligned_data = np.clip(aligned_data, -1, 1)
    
    # Export
    sf.write(output_filepath, aligned_data, sample_rate, subtype=subtype, format='WAV')
    
    # Verify
    result = verify_stem(output_filepath, sample_rate, subtype, target_duration)
    if result['success']:
        return {
            'success': True,
            'strategy': strategy,
            'original_duration': current_duration,
            'aligned_duration': result['info'].duration
        }
    else:
        return result

# Apply duration alignment to all stems
print("\n=== Aligning stem durations ===")
aligned_stems = []

for stem_name in STEM_NAMES:
    input_file = f'{stem_name}_stem.wav'
    output_file = f'{stem_name}_aligned.wav'
    
    # Determine stem type for strategy selection
    stem_type_map = {
        'bass': 'rhythmic',
        'guitars': 'melodic',
        'synths': 'ambient',
        'bridge': 'melodic'
    }
    stem_type = stem_type_map.get(stem_name, 'melodic')
    
    print(f"Aligning {stem_name} (type: {stem_type})...")
    result = align_stem_duration(
        input_file, output_file, TARGET_DURATION, SAMPLE_RATE,
        subtype=SUBTYPE, strategy='auto', stem_type=stem_type
    )
    
    if result['success']:
        if result['strategy'] != 'none':
            print(f"✓ {stem_name} aligned: {result['original_duration']:.2f}s → {result['aligned_duration']:.2f}s ({result['strategy']})")
        else:
            print(f"✓ {stem_name} already aligned")
        aligned_stems.append(output_file)
    else:
        print(f"✗ {stem_name} alignment FAILED: {result['error']}")
        raise RuntimeError(f"Stem alignment failed for {stem_name}: {result['error']}")

# Align drums separately
drums_aligned = 'drums_aligned.wav'
print(f"Aligning drums (type: percussion)...")
drums_result = align_stem_duration(
    'drums_stem.wav', drums_aligned, TARGET_DURATION, SAMPLE_RATE,
    subtype=SUBTYPE, strategy='auto', stem_type='percussion'
)
if drums_result['success']:
    if drums_result['strategy'] != 'none':
        print(f"✓ Drums aligned: {drums_result['original_duration']:.2f}s → {drums_result['aligned_duration']:.2f}s ({drums_result['strategy']})")
    else:
        print(f"✓ Drums already aligned")
    aligned_stems.append(drums_aligned)
else:
    raise RuntimeError(f"Drums alignment failed: {drums_result['error']}")

# Final duration verification - all stems must match
print("\n=== Verifying duration alignment ===")
final_durations = {}
for stem_file in aligned_stems:
    info = sf.info(stem_file)
    stem_name = os.path.basename(stem_file).replace('_aligned.wav', '')
    final_durations[stem_name] = info.duration
    duration_diff = abs(info.duration - TARGET_DURATION)
    
    if duration_diff > 0.5:
        print(f"✗ WARNING: {stem_name} duration mismatch: {info.duration:.2f}s vs target {TARGET_DURATION:.2f}s")
    else:
        print(f"✓ {stem_name}: {info.duration:.2f}s (Δ{duration_diff:.2f}s)")

max_duration_diff = max(abs(d - TARGET_DURATION) for d in final_durations.values())
if max_duration_diff > 0.5:
    raise RuntimeError(f"Duration alignment incomplete: max deviation {max_duration_diff:.2f}s exceeds tolerance")
print(f"\nAll stems aligned within tolerance (max deviation: {max_duration_diff:.2f}s)")
```

## Step 6: Apply Effects with Verification

Process each stem and verify the output:

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
    result = verify_stem(output_filepath, sample_rate, subtype, TARGET_DURATION)
    return result, processed

print("\n=== Applying effects to all stems ===")
processed_stems = []

for stem_name in STEM_NAMES:
    input_file = f'{stem_name}_aligned.wav'
    output_file = f'{stem_name}_processed.wav'
    
    print(f"Processing {stem_name}...")
    result, _ = apply_effects_and_verify(input_file, output_file, SAMPLE_RATE, SUBTYPE)
    
    if result['success']:
        print(f"✓ {stem_name} processed and verified")
        processed_stems.append(output_file)
    else:
        print(f"✗ {stem_name} processing FAILED: {result['error']}")
        raise RuntimeError(f"Effects processing failed for {stem_name}")

# Process drums separately
drums_output = 'drums_processed.wav'
drums_result, _ = apply_effects_and_verify('drums_aligned.wav', drums_output, SAMPLE_RATE, SUBTYPE)
if drums_result['success']:
    print(f"✓ Drums processed and verified")
    processed_stems.append(drums_output)
else:
    raise RuntimeError(f"Drums processing failed: {drums_result['error']}")
```

## Step 7: Export Master Track

Mix all verified stems into master track:

```python
def create_master_track(stem_files, output_filepath, sample_rate, subtype):
    """Create master track from verified stems with gain staging."""
    # Load first stem to get dimensions
    first_data, sr = sf.read(stem_files[0])
    master_audio = np.zeros(len(first_data), dtype=np.float32)
    
    # Verify all stems have matching length
    for stem_file in stem_files:
        data, _ = sf.read(stem_file)
        if len(data) != len(first_data):
            raise ValueError(f"Stem length mismatch: {stem_file} has {len(data)} frames, expected {len(first_data)}")
    
    # Mix all stems with gain staging
    gain_per_stem = 0.4  # Prevent clipping with 5 stems
   
    for i, stem_file in enumerate(stem_files):
        data, sr = sf.read(stem_file)
        master_audio += data * gain_per_stem
        print(f"  Mixed {os.path.basename(stem_file)} (gain: {gain_per_stem})")
    
    # Apply master bus limiting
    master_audio = np.clip(master_audio, -1, 1)
    
    # Soft clip for warmth
    master_audio = np.tanh(master_audio * 1.2) / 1.2
    
    # Export
    sf.write(output_filepath, master_audio, sample_rate, subtype=subtype, format='WAV')
    
    # Verify
    info = sf.info(output_filepath)
    print(f"Master exported: {info.duration:.2f}s @ {info.samplerate}Hz, {info.channels}ch")
    
    return output_filepath, master_audio

print("\n=== Creating master track ===")
master_filepath, master_data = create_master_track(processed_stems, 'master.wav', SAMPLE_RATE, SUBTYPE)
```

## Step 8: Archive and Final Verification

Package deliverables with comprehensive checks:

```python
import json
from datetime import datetime

def create_archive_manifest(stem_files, master_file, output_dir='deliverables'):
    """Create archive manifest with comprehensive verification."""
    os.makedirs(output_dir, exist_ok=True)
    
    manifest = {
        'created': datetime.now().isoformat(),
        'target_duration': TARGET_DURATION,
        'sample_rate': SAMPLE_RATE,
        'subtype': SUBTYPE,
        'stems': [],
        'master': None,
        'verification': {
            'all_stems_aligned': True,
            'all_stems_verified': True,
            'master_verified': True
        }
    }
    
    # Verify each stem
    for stem_file in stem_files:
        if not os.path.exists(stem_file):
            manifest['verification']['all_stems_verified'] = False
            continue
        
        info = sf.info(stem_file)
        stem_name = os.path.basename(stem_file)
        duration_diff = abs(info.duration - TARGET_DURATION)
        
        stem_info = {
            'file': stem_name,
            'duration': info.duration,
            'sample_rate': info.samplerate,
            'channels': info.channels,
            'duration_aligned': duration_diff < 0.5
        }
        manifest['stems'].append(stem_info)
        
        if duration_diff >= 0.5:
            manifest['verification']['all_stems_aligned'] = False
            print(f"WARNING: {stem_name} duration misaligned by {duration_diff:.2f}s")
    
    # Verify master
    if os.path.exists(master_file):
        info = sf.info(master_file)
        manifest['master'] = {
            'file': os.path.basename(master_file),
            'duration': info.duration,
            'sample_rate': info.samplerate,
            'channels': info.channels,
            'subtype': info.subtype
        }
        
        # Check master duration matches target
        if abs(info.duration - TARGET_DURATION) > 1.0:
            manifest['verification']['master_verified'] = False
            print(f"WARNING: Master duration {info.duration:.2f}s differs from target {TARGET_DURATION:.2f}s")
    else:
        manifest['verification']['master_verified'] = False
    
    # Save manifest
    manifest_path = os.path.join(output_dir, 'manifest.json')
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f, indent=2)
    
    # Copy files to deliverables
    import shutil
    for stem_file in stem_files:
        shutil.copy(stem_file, output_dir)
    shutil.copy(master_file, output_dir)
    
    return manifest_path, manifest

print("\n=== Creating archive ===")
manifest_path, manifest = create_archive_manifest(processed_stems, master_filepath)
print(f"Archive manifest created: {manifest_path}")

# Final summary
print("\n" + "="*60)
print("PRODUCTION COMPLETE")
print("="*60)
print(f"Target duration: {TARGET_DURATION:.2f}s")
print(f"Sample rate: {SAMPLE_RATE}Hz")
print(f"Stems processed: {len(processed_stems)}")
print(f"All stems aligned: {manifest['verification']['all_stems_aligned']}")
print(f"Master verified: {manifest['verification']['master_verified']}")
print(f"Deliverables: ./deliverables/")
print("="*60)
```

## Troubleshooting Duration Mismatches

### Common Causes
1. **Different sample rates**: Ensure all stems use the same sample rate
2. **Incorrect frame calculations**: Verify `frames = int(duration * sample_rate)` calculations
3. **Off-by-one errors**: Check array indexing and loop boundaries
4. **Resampling artifacts**: When converting between sample rates, use high-quality resampling

### Strategy Selection Guide

**Use zero-padding when:**
- Stem is ambient/pad/drone content
- Short duration mismatch (< 10% of total)
- Quick turnaround needed

**Use looping when:**
- Stem is rhythmic (drums, percussion, rhythmic bass)
- Content has clear loop points
- Loop length divides evenly into target duration

**Use crossfade extension when:**
- Stem is melodic (vocals, guitars, synths)
- Quality is priority over speed
- Significant duration extension needed

**Use time-stretch when:**
- Content cannot be looped or padded
- Pitch must be preserved
- High-quality processing is available (e.g., librubberband, elasticsearch)

### Verification Checklist
- [ ] All stems have matching sample rate
- [ ] All stems within 0.5s of target duration
- [ ] No clipping in any stem (-1 to 1 range)
- [ ] Master track duration matches target
- [ ] Archive manifest generated with verification status
