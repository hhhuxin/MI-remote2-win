# Xiaomi Bluetooth Remote 2 Windows

Windows client for Xiaomi Bluetooth Remote 2 and 2 Pro (VID `2717`, PID `32B8`). The runnable client follows the upstream community preview architecture: a dedicated Win32 message-only window and a background Raw Input thread. It does not open the `\\kbd` HID handle because Windows `kbdhid` owns that interface and returns access denied.

Run with:

```powershell
py XiaomiRemote2_Windows.py
```

The recorder preserves complete Raw Input keyboard/HID payloads, exact device paths, unknown events, and `DOWN/REPEAT/UP` edges. `保存` writes `xiaomi_remote2_test.json` and `xiaomi_remote2_raw.log`.

The Windows client recognizes the 13-button RC003 profile (including the voice
key) across keyboard and Consumer HID reports. The mapping panel stores edits in
`xiaomi_remote2_mapping.json`. Defaults emit isolated F13-F24/OEM-8 synthetic
keys; the listener uses `RIDEV_INPUTSINK` without `RIDEV_NOLEGACY`, so it does not
rewrite or suppress physical keyboard input. Standard arrows/media keys remain
available as explicit mapping choices.

Voice setup (optional): install the Windows packages with
`py -3 -m pip install -r requirements-windows.txt`, install/pair a virtual
microphone such as VB-CABLE, launch the app, select `CABLE Input`, and click
`连接语音`. Hold the remote voice key while speaking; release it to stop. The
voice path uses the ATVV service and characteristics documented by the upstream
RC003 implementation and does not save audio to disk.

The borrowed protocol findings are:

- HID identity: `VID 0x2717`, `PID 0x32B8`; BLE HID paths use `Dev_VID&012717_PID&32B8`.
- Report usages match the shared Xiaomi profile: OK `0x28`, Home `0x4A`, arrows `0x4F`-`0x52`, menu `0x65`, power `0x66`, volume `0x80/0x81`, voice `0x3E`.
- Voice transport uses the ATVV GATT service `AB5E0001-5A21-4F05-BC7D-AF01F617B664`, with TX/audio/control characteristics ending in `0002/0003/0004`. Windows voice control is available when the WinRT and audio dependencies are installed; real-device acceptance still requires a live RC001/RC003 session.

Install optional WinRT packages for BLE inspection:

```powershell
py -3 -m pip install -r requirements-windows.txt
```
