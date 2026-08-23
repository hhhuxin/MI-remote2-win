"""Pure Xiaomi ATVV protocol helpers shared by RC001 and RC003."""
from __future__ import annotations

import math
from dataclasses import dataclass

VOICE_SERVICE_UUID = "AB5E0001-5A21-4F05-BC7D-AF01F617B664"
VOICE_TX_UUID = "AB5E0002-5A21-4F05-BC7D-AF01F617B664"
VOICE_AUDIO_UUID = "AB5E0003-5A21-4F05-BC7D-AF01F617B664"
VOICE_CONTROL_UUID = "AB5E0004-5A21-4F05-BC7D-AF01F617B664"
GET_CAPABILITIES_V10 = bytes((0x0A, 0x01, 0x00, 0x00, 0x03, 0x03))


def mic_open_command(version: int = 0x0100) -> bytes:
    return bytes((0x0C, 0x00)) if version >= 0x0100 else bytes((0x0C, 0x00, 0x00))


def mic_close_command(version: int, session_id: int = 0) -> bytes:
    return bytes((0x0D, session_id & 0xFF)) if version >= 0x0100 else bytes((0x0D,))


@dataclass(frozen=True)
class Capabilities:
    version: int
    codecs: int
    interaction: int
    frame_size: int
    selected_codec: int
    sample_rate: int

    @classmethod
    def parse(cls, data: bytes) -> "Capabilities | None":
        if len(data) < 7 or data[0] != 0x0B: return None
        version = data[1] << 8 | data[2]
        if version >= 0x0100:
            codecs, interaction = data[3], data[4]
            if codecs == 0 and len(data) >= 9 and data[4] & 3: codecs, interaction = data[4], 3
        else:
            if len(data) < 9: return None
            codecs, interaction = data[4], 0
        frame = data[5] << 8 | data[6]
        codec = 2 if codecs & 2 else 1
        return cls(version, codecs, interaction, frame or 120, codec, 16000 if codec == 2 else 8000)


class IMAADPCMDecoder:
    _steps = (7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,2272,2499,2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,7132,7845,8630,9493,10442,11487,12635,13899,15289,16818,18500,20350,22385,24623,27086,29794,32767)
    _index = (-1,-1,-1,-1,2,4,6,8)
    def __init__(self): self.predictor, self.step_index = 0, 0
    def reset(self): self.predictor, self.step_index = 0, 0
    def decode(self, data: bytes) -> list[int]:
        out=[]
        for byte in data:
            for nibble in (byte >> 4, byte & 15):
                step=self._steps[self.step_index]; diff=step>>3
                if nibble&1: diff += step>>2
                if nibble&2: diff += step>>1
                if nibble&4: diff += step
                self.predictor += -diff if nibble&8 else diff
                self.predictor=max(-32768,min(32767,self.predictor)); self.step_index=max(0,min(88,self.step_index+self._index[nibble&7])); out.append(self.predictor)
        return out
