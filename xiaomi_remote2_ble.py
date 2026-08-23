"""Optional WinRT BLE/ATVV inspector for Xiaomi Remote 2 and 2 Pro.

This module deliberately reports unavailable APIs instead of inventing a BLE
connection. It uses the same WinRT call shape as the upstream Windows RC003
candidate and accepts both RC001 and RC003 advertised names.
"""
from __future__ import annotations
import asyncio, struct, threading, time, uuid
from dataclasses import dataclass
from xiaomi_remote2_protocol import *

DEVICE_NAMES = {"mi rc", "xiaomi bluetooth remote 2", "xiaomi bluetooth remote 2 pro", "小米蓝牙语音遥控器"}

class WinRTUnavailableError(RuntimeError): pass

@dataclass
class BLEInspection:
    name: str
    device_id: str
    status: str
    services: list[dict]

def _modules():
    try:
        from winrt.windows.devices.bluetooth import BluetoothLEDevice, BluetoothCacheMode
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattCommunicationStatus, GattClientCharacteristicConfigurationDescriptorValue
        from winrt.windows.devices.enumeration import DeviceInformation
        from winrt.windows.storage.streams import DataWriter
    except ImportError as exc:
        raise WinRTUnavailableError("安装 requirements-windows.txt 后才能访问 Windows BLE WinRT API") from exc
    return BluetoothLEDevice, BluetoothCacheMode, GattCommunicationStatus, GattClientCharacteristicConfigurationDescriptorValue, DeviceInformation, DataWriter

async def inspect_paired_remotes() -> list[BLEInspection]:
    BluetoothLEDevice, CacheMode, Status, _CCCD, DeviceInformation, _DataWriter = _modules()
    selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
    infos = await DeviceInformation.find_all_async_aqs_filter(selector)
    result=[]
    for info in infos:
        name=(getattr(info,"name","") or "").strip()
        if name.casefold() not in {n.casefold() for n in DEVICE_NAMES}: continue
        try:
            device=await BluetoothLEDevice.from_id_async(info.id)
            svc_result=await device.get_gatt_services_for_uuid_with_cache_mode_async(uuid.UUID(VOICE_SERVICE_UUID), CacheMode.UNCACHED)
            services=[]
            for svc in getattr(svc_result,"services",[]) or []:
                chars=await svc.get_characteristics_with_cache_mode_async(CacheMode.UNCACHED)
                services.append({"uuid":str(svc.uuid),"status":str(getattr(chars,"status","unknown")),"characteristics":[str(c.uuid) for c in (getattr(chars,"characteristics",[]) or [])]})
            result.append(BLEInspection(name, info.id, "ATVV service found" if services else "ATVV service not found", services))
            try: device.close()
            except Exception: pass
        except Exception as exc:
            result.append(BLEInspection(name, info.id, f"ERROR {type(exc).__name__}", []))
    return result

def inspect_sync() -> list[BLEInspection]:
    return asyncio.run(inspect_paired_remotes())


class VoiceAudioUnavailableError(RuntimeError):
    pass


class PCMOutput:
    """Small sounddevice sink; it never changes the Windows default device."""

    def __init__(self, device_name: str | None = None):
        self.device_name = device_name
        self.stream = None

    @staticmethod
    def list_devices() -> list[str]:
        try:
            import sounddevice as sd
            return [str(d["name"]) for d in sd.query_devices() if d.get("max_output_channels", 0) > 0]
        except ImportError as exc:
            raise VoiceAudioUnavailableError("请安装 sounddevice，并安装 VB-CABLE 后选择 CABLE Input") from exc
        except Exception as exc:
            raise VoiceAudioUnavailableError("无法枚举 Windows 音频输出设备") from exc

    def start(self):
        try:
            import sounddevice as sd
            if not self.device_name:
                raise VoiceAudioUnavailableError("请先选择语音输出设备（建议选择 CABLE Input）")
            self.stream = sd.RawOutputStream(samplerate=16000, channels=1, dtype="int16", device=self.device_name, blocksize=0)
            self.stream.start()
        except VoiceAudioUnavailableError:
            raise
        except ImportError as exc:
            raise VoiceAudioUnavailableError("缺少 sounddevice，请安装 requirements-windows.txt") from exc
        except Exception as exc:
            raise VoiceAudioUnavailableError(f"无法打开音频输出设备：{exc}") from exc

    def write(self, samples: list[int]):
        if self.stream is None or not samples:
            return
        self.stream.write(struct.pack("<" + "h" * len(samples), *samples))

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            finally:
                self.stream = None


class ATVVVoiceController:
    """Minimal WinRT ATVV session with a persistent asyncio loop."""

    def __init__(self, status_callback=None):
        self.status_callback = status_callback or (lambda _msg: None)
        self.loop = None; self.thread = None; self.ready = threading.Event()
        self.stop_event = None; self.device = None; self.service = None
        self.tx = None; self.audio = None; self.control = None
        self.audio_token = None; self.control_token = None
        self.version = 0x0100; self.frame_size = 120; self.caps = None
        self.decoder = IMAADPCMDecoder(); self.pending = bytearray(); self.sync = None
        self.mic_open = False; self.output = PCMOutput(); self.connected = False

    def _status(self, text):
        try: self.status_callback(text)
        except Exception: pass

    def _ensure_loop(self):
        if self.thread and self.thread.is_alive(): return
        self.ready.clear()
        self.thread = threading.Thread(target=self._loop_thread, name="xiaomi-atvv-loop", daemon=True); self.thread.start()
        if not self.ready.wait(5): raise RuntimeError("BLE 音频线程未启动")

    def _loop_thread(self):
        self.loop = asyncio.new_event_loop(); asyncio.set_event_loop(self.loop)
        self.stop_event = asyncio.Event(); self.ready.set()
        self.loop.run_until_complete(self.stop_event.wait())
        self.loop.close()

    def _call(self, coro, timeout=15):
        self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def connect(self, device_id: str | None = None):
        return self._call(self._connect(device_id))

    async def _connect(self, device_id):
        if self.connected: return
        BluetoothLEDevice, CacheMode, Status, CCCD, DeviceInformation, DataWriter = _modules()
        selector = BluetoothLEDevice.get_device_selector_from_pairing_state(True)
        infos = await DeviceInformation.find_all_async_aqs_filter(selector)
        candidates = [i for i in infos if (getattr(i, "name", "") or "").strip().casefold() in {n.casefold() for n in DEVICE_NAMES}]
        if device_id: candidates = [i for i in candidates if getattr(i, "id", "") == device_id]
        if not candidates: raise RuntimeError("未找到已配对的小米蓝牙语音遥控器")
        info = candidates[0]
        self._status(f"BLE: 正在连接 {getattr(info, 'name', '')}")
        self.device = await BluetoothLEDevice.from_id_async(info.id)
        result = await self.device.get_gatt_services_for_uuid_with_cache_mode_async(uuid.UUID(VOICE_SERVICE_UUID), CacheMode.UNCACHED)
        if result.status != Status.SUCCESS or not result.services: raise RuntimeError("未找到 ATVV 语音服务")
        self.service = result.services[0]
        chars_result = await self.service.get_characteristics_with_cache_mode_async(CacheMode.UNCACHED)
        if chars_result.status != Status.SUCCESS: raise RuntimeError("ATVV 特征发现失败")
        by_uuid = {str(c.uuid).casefold(): c for c in chars_result.characteristics}
        self.tx = by_uuid.get(VOICE_TX_UUID.casefold()); self.audio = by_uuid.get(VOICE_AUDIO_UUID.casefold()); self.control = by_uuid.get(VOICE_CONTROL_UUID.casefold())
        if not all((self.tx, self.audio, self.control)): raise RuntimeError("ATVV 音频/控制特征不完整")
        self.audio_token = self.audio.add_value_changed(self._audio_changed)
        self.control_token = self.control.add_value_changed(self._control_changed)
        for characteristic in (self.audio, self.control):
            status = await characteristic.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
            if status != Status.SUCCESS: raise RuntimeError("ATVV 通知订阅失败")
        self.connected = True
        self._status("BLE: 已连接，正在协商语音能力")
        await self._write(GET_CAPABILITIES_V10)

    async def _write(self, data: bytes):
        _BluetoothLEDevice, _CacheMode, Status, _CCCD, _DeviceInformation, DataWriter = _modules()
        writer = DataWriter(); writer.write_bytes(bytes(data)); result = await self.tx.write_value_with_result_async(writer.detach_buffer())
        if result.status != Status.SUCCESS: raise RuntimeError(f"ATVV 写入失败：{result.status}")

    def _control_changed(self, _sender, args):
        payload = bytes(args.characteristic_value)
        if not payload: return
        if payload[0] == 0x0B:
            self.caps = Capabilities.parse(payload)
            if self.caps is None: self._status("BLE: 能力协商数据无效"); return
            self.version = self.caps.version; self.frame_size = self.caps.frame_size
            if self.caps.sample_rate != 16000: self._status(f"BLE: 不支持 {self.caps.sample_rate} Hz，仅支持 16 kHz")
            else: self._status(f"BLE: 语音就绪（{self.caps.sample_rate} Hz，帧长 {self.frame_size}）")
        elif payload[0] == 0x04:
            self.decoder.reset(); self.pending.clear(); self.sync = None; self.mic_open = True; self._status("语音: 设备开始发送")
        elif payload[0] == 0x00:
            self.mic_open = False; self._status("语音: 设备停止发送")
        elif payload[0] == 0x0A and len(payload) >= 7:
            self.sync = (int.from_bytes(payload[4:6], "big", signed=True), payload[6])

    def _audio_changed(self, _sender, args):
        if not self.mic_open: return
        self.pending.extend(bytes(args.characteristic_value))
        while len(self.pending) >= self.frame_size:
            frame = bytes(self.pending[:self.frame_size]); del self.pending[:self.frame_size]
            if self.sync is not None: self.decoder.reset(*self.sync); self.sync = None
            try: self.output.write(postprocess(self.decoder.decode(frame)))
            except Exception as exc: self._status(f"语音: 音频输出失败（{exc}）")

    def press(self):
        if self.connected:
            try:
                if self.output.stream is None: self.output.start()
                self._call(self._write(mic_open_command(self.version)), 5); self._status("语音: 正在打开麦克风")
            except Exception as exc:
                self._status(f"语音: 无法启动音频（{exc}）")
        else: self._status("语音: 尚未连接 BLE")

    def release(self):
        if self.connected:
            try: self._call(self._write(mic_close_command(self.version)), 5)
            except Exception as exc: self._status(f"语音: 关闭失败（{exc}）")
        self.mic_open = False; self.output.stop()

    async def _close_async(self):
        if self.audio is not None:
            try:
                _BluetoothLEDevice, _CacheMode, _Status, CCCD, _DeviceInformation, _DataWriter = _modules()
                await self.audio.write_client_characteristic_configuration_descriptor_async(CCCD.NONE)
            except Exception: pass
            try:
                self.audio.remove_value_changed(self.audio_token)
            except Exception: pass
        if self.control is not None:
            try:
                _BluetoothLEDevice, _CacheMode, _Status, CCCD, _DeviceInformation, _DataWriter = _modules()
                await self.control.write_client_characteristic_configuration_descriptor_async(CCCD.NONE)
            except Exception: pass
            try:
                self.control.remove_value_changed(self.control_token)
            except Exception: pass
        for item in (self.service, self.device):
            try:
                if item is not None: item.close()
            except Exception: pass
        self.audio = self.control = self.tx = self.service = self.device = None
        self.connected = False; self.mic_open = False

    def close(self):
        if not self.thread or not self.thread.is_alive(): return
        try: self.release(); self._call(self._close_async(), 8)
        except Exception: pass
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(timeout=3)
