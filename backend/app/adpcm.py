"""Bounded Intel/DVI IMA 4-bit decoding for ephemeral microphone uploads.

Codes are high nibble first, matching audioop, not the WAV block container.
The initial state describes the sample immediately before the first code.
"""
from .errors import XiaoNuoError

STEPS = (7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
         34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130,
         143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408,
         449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166,
         1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
         3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845,
         8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500,
         20350, 22385, 24623, 27086, 29794, 32767)
INDEX_DELTA = (-1, -1, -1, -1, 2, 4, 6, 8)
ENCODING = 'ima_adpcm4'
MAX_ENCODED_BYTES = 6 * 16000 // 2
MAX_PCM_BYTES = 8 * 16000 * 2  # Downlink replies can be up to eight seconds.


def validate_state(predictor, step_index):
    if (type(predictor) is not int or not -32768 <= predictor <= 32767
            or type(step_index) is not int or not 0 <= step_index < len(STEPS)):
        raise XiaoNuoError('NETWORK_AUDIO_INVALID_ADPCM_STATE', 422)


def decode(data, predictor=0, step_index=0):
    """Return bounded little-endian PCM16 and the final decoder state."""
    validate_state(predictor, step_index)
    if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_ENCODED_BYTES:
        raise XiaoNuoError('NETWORK_AUDIO_INVALID_ADPCM', 422)
    pcm = bytearray(len(data) * 4)
    offset = 0
    for byte in data:
        for code in (byte >> 4, byte & 15):
            step = STEPS[step_index]
            difference = step >> 3
            if code & 4:
                difference += step
            if code & 2:
                difference += step >> 1
            if code & 1:
                difference += step >> 2
            predictor = max(-32768, min(32767, predictor + (-difference if code & 8 else difference)))
            step_index = max(0, min(88, step_index + INDEX_DELTA[code & 7]))
            pcm[offset] = predictor & 255
            pcm[offset + 1] = (predictor >> 8) & 255
            offset += 2
    return bytes(pcm), (predictor, step_index)


def encode(pcm, predictor=0, step_index=0, *, max_seconds=8):
    """Encode bounded little-endian PCM16; high nibble is the first sample."""
    validate_state(predictor, step_index)
    if (type(max_seconds) is not int or max_seconds not in {8,20,60}
            or not isinstance(pcm, (bytes, bytearray)) or not pcm
            or len(pcm) % 2 or len(pcm) > max_seconds*16000*2):
        raise XiaoNuoError('NETWORK_AUDIO_INVALID_PCM', 422)
    samples = len(pcm) // 2
    encoded = bytearray((samples + 1) // 2)
    for sample_index in range(samples):
        sample = int.from_bytes(pcm[sample_index * 2:sample_index * 2 + 2], 'little', signed=True)
        step = STEPS[step_index]
        difference = sample - predictor
        code = 0
        if difference < 0:
            code = 8
            difference = -difference
        if difference >= step:
            code |= 4
            difference -= step
        step >>= 1
        if difference >= step:
            code |= 2
            difference -= step
        step >>= 1
        if difference >= step:
            code |= 1
        step = STEPS[step_index]
        delta = step >> 3
        if code & 4:
            delta += step
        if code & 2:
            delta += step >> 1
        if code & 1:
            delta += step >> 2
        predictor = max(-32768, min(32767, predictor + (-delta if code & 8 else delta)))
        step_index = max(0, min(88, step_index + INDEX_DELTA[code & 7]))
        shift = 4 if sample_index % 2 == 0 else 0
        encoded[sample_index // 2] |= code << shift
    return bytes(encoded), (predictor, step_index)
