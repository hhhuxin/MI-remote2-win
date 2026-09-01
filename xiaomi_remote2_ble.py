"""Optional WinRT BLE/ATVV inspector for Xiaomi Remote 2 and 2 Pro.

This module deliberately reports unavailable APIs instead of inventing a BLE
connection. It uses the same WinRT call shape as the upstream Windows RC003
candidate and accepts both RC001 and RC003 advertised names.
"""
from __future__ import annotations
import asyncio, re, struct, threading, time, uuid
from dataclasses import dataclass
from xiaomi_remote2_protocol import *

DEVICE_NAMES = {"mi rc", "xiaomi bluetooth remote 2", "xiaomi bluetooth remote 2 pro", "小米蓝牙语音遥控器"}

def _is_remote_name(name: str) -> bool:
    """Accept Windows' varying localized/model suffixes; ATVV service is checked later."""
    value = (name or "").strip().casefold()
    return value in {n.casefold() for n in DEVICE_NAMES} or ("xiaomi" in value and "remote" in value) or value.startswith("mi rc") or "小米蓝牙" in value

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
        if not _is_remote_name(name): continue
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
        self.device_index = None

    @staticmethod
    def _display_name(index: int, device, hostapis) -> str:
        hostapi = hostapis[int(device["hostapi"])]["name"]
        return f'{device["name"]} [{hostapi}] (#{index})'

    @staticmethod
    def _device_id(selection: str):
        """Return the stable index embedded in a UI label, or the legacy name."""
        match = re.search(r"\s\(#(\d+)\)$", selection)
        return int(match.group(1)) if match else selection

    @staticmethod
    def list_devices() -> list[str]:
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            return [PCMOutput._display_name(i, d, hostapis) for i, d in enumerate(devices) if d.get("max_output_channels", 0) > 0]
        except ImportError as exc:
            raise VoiceAudioUnavailableError("请安装 sounddevice，并安装 VB-CABLE 后选择 CABLE Input") from exc
        except Exception as exc:
            raise VoiceAudioUnavailableError("无法枚举 Windows 音频输出设备") from exc

    @staticmethod
    def preferred_device(devices: list[str]) -> str:
        """Prefer the active VB-CABLE playback endpoint, including 16ch mode."""
        if not devices:
            return ""
        try:
            import sounddevice as sd
            default_output = int(sd.default.device[1])
            selected = next((name for name in devices if PCMOutput._device_id(name) == default_output), "")
            if selected.casefold().startswith(("cable input", "cable in")):
                return selected
        except Exception:
            pass
        cable = [name for name in devices if name.casefold().startswith(("cable input", "cable in"))]
        return cable[0] if cable else devices[0]

    def start(self):
        try:
            import sounddevice as sd
            if not self.device_name:
                raise VoiceAudioUnavailableError("请先选择语音输出设备（建议选择 CABLE Input）")
            if self.device_name.strip().casefold().startswith("cable output"):
                raise VoiceAudioUnavailableError("方向选反了：程序请选择 CABLE Input；Windows 麦克风/测试麦克风请选择 CABLE Output")
            # Windows exposes the same endpoint through several host APIs.  A
            # plain name is ambiguous, so UI selections carry a stable index.
            self.device_index = self._device_id(self.device_name)
            sd.check_output_settings(device=self.device_index, samplerate=16000, channels=1, dtype="int16")
            # Use two channels so recording applications configured for the
            # usual stereo CABLE Output endpoint cannot miss a mono channel.
            self.stream = sd.RawOutputStream(samplerate=16000, channels=2, dtype="int16", device=self.device_index, blocksize=0)
            self.stream.start()
        except VoiceAudioUnavailableError:
            raise
        except ImportError as exc:
            raise VoiceAudioUnavailableError("缺少 sounddevice，请安装 requirements-windows.txt") from exc
        except Exception as exc:
            raise VoiceAudioUnavailableError(f"无法打开音频输出设备：{exc}") from exc

    def write(self, samples: list[int]):
        if self.stream is None or not samples:
            raise VoiceAudioUnavailableError("音频输出流未启动")
        interleaved = [sample for value in samples for sample in (value, value)]
        try:
            self.stream.write(struct.pack("<" + "h" * len(interleaved), *interleaved))
        except Exception as exc:
            self.stop()
            raise VoiceAudioUnavailableError(f"音频输出流写入失败：{exc}") from exc

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop(); self.stream.close()
            finally:
                self.stream = None
                self.device_index = None


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
        self.audio_frames = 0; self.audio_notifications = 0; self.audio_bytes = 0
        self.last_audio_at = 0.0
        self._device_id = None
        self._connection_token = None
        self._watchdog_task = None
        self._recovering = False

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
        candidates = [i for i in infos if _is_remote_name(getattr(i, "name", ""))]
        if device_id: candidates = [i for i in candidates if getattr(i, "id", "") == device_id]
        if not candidates: raise RuntimeError("未找到已配对的小米蓝牙语音遥控器")
        info = candidates[0]
        self._status(f"BLE: 正在连接 {getattr(info, 'name', '')}")
        self.device = await BluetoothLEDevice.from_id_async(info.id)
        self._device_id = info.id
        try:
            self._connection_token = self.device.add_connection_status_changed(self._connection_status_changed)
        except Exception:
            self._connection_token = None
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
        self.last_audio_at = time.monotonic()
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())
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
        self.audio_notifications += 1
        # WinRT projections have exposed this value as either
        # ``characteristic_value`` or ``value`` across package versions.
        raw = getattr(args, "characteristic_value", None)
        if raw is None:
            raw = getattr(args, "value", None)
        if raw is None:
            self._status("语音: 收到音频通知但无法读取数据字段")
            return
        try:
            payload = bytes(raw)
        except Exception as exc:
            self._status(f"语音: 音频通知转换失败（{exc}）")
            return
        if not payload:
            return
        self.audio_bytes += len(payload)
        self.last_audio_at = time.monotonic()
        if self.audio_notifications == 1:
            self._status(f"语音: 收到 BLE 音频通知（{len(payload)} 字节）")
        # Some remotes start audio notifications before the 0x04 control ack.
        # Once the user pressed the key, accept those packets instead of
        # silently dropping them.
        if not self.mic_open:
            return
        self.audio_frames += 1
        self.pending.extend(payload)
        while len(self.pending) >= self.frame_size:
            frame = bytes(self.pending[:self.frame_size]); del self.pending[:self.frame_size]
            if self.sync is not None: self.decoder.reset(*self.sync); self.sync = None
            try:
                samples = postprocess(self.decoder.decode(frame))
                if self.audio_frames == 1:
                    peak = max(abs(sample) for sample in samples) if samples else 0
                    self._status(f"语音: PCM 已解码，峰值 {peak}，输出设备 #{self.output.device_index}")
                self.output.write(samples)
                if self.audio_frames == 1:
                    self._status("语音: 音频采集并发送")
            except Exception as exc:
                self._status(f"语音: 音频流异常（{exc}），正在恢复")
                self._schedule_recovery()

    def press(self):
        if self.connected:
            try:
                self.audio_frames = 0
                self.audio_notifications = 0; self.audio_bytes = 0
                if self.output.stream is None: self.output.start()
                self.last_audio_at = time.monotonic()
                self.mic_open = True
                self._call(self._write(mic_open_command(self.version)), 5); self._status("语音: 等待音频")
            except Exception as exc:
                self._status(f"语音: 无法启动音频（{exc}）")
        else: self._status("语音: 尚未连接 BLE")

    def release(self):
        if self.connected:
            try: self._call(self._write(mic_close_command(self.version)), 5)
            except Exception as exc: self._status(f"语音: 关闭失败（{exc}）")
        self.mic_open = False; self.output.stop()
        self._status("语音: 已停止")

    async def _close_async(self):
        if self._watchdog_task is not None:
            if self._watchdog_task is not asyncio.current_task():
                self._watchdog_task.cancel()
            self._watchdog_task = None
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
        self.connected = False; self.mic_open = False; self._connection_token = None

    def _connection_status_changed(self, sender, _args):
        status = str(getattr(sender, "connection_status", "unknown"))
        if "connected" not in status.casefold():
            self._status(f"BLE: 连接状态异常（{status}），正在恢复")
            self._schedule_recovery()

    def _schedule_recovery(self):
        if self.loop and self.loop.is_running() and not self._recovering:
            asyncio.run_coroutine_threadsafe(self._recover(), self.loop)

    async def _watchdog(self):
        while self.connected:
            await asyncio.sleep(2)
            if self.mic_open and self.last_audio_at and time.monotonic() - self.last_audio_at > 5:
                self._status("语音: 超过 5 秒未收到音频帧，正在恢复")
                await self._recover()

    async def _recover(self):
        if self._recovering or not self.connected:
            return
        self._recovering = True
        was_open = self.mic_open
        try:
            self.mic_open = False
            self.output.stop()
            device_id = self._device_id
            await self._close_async()
            await asyncio.sleep(0.5)
            await self._connect(device_id)
            if was_open:
                self.output.start()
                self.mic_open = True
                await self._write(mic_open_command(self.version))
                self._status("语音: 音频链路已自动恢复")
        except Exception as exc:
            self.output.stop()
            self._status(f"语音: 自动恢复失败（{exc}），等待下一次检测")
        finally:
            self._recovering = False

    def close(self):
        if not self.thread or not self.thread.is_alive(): return
        try: self.release(); self._call(self._close_async(), 8)
        except Exception: pass
        if self.loop and self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(timeout=3)
