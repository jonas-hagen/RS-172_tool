# RS-172 serial protocol

The RS PRO RS-172 is a rebadged **CEM DT-172**, which is also sold as the **Voltcraft DL-141TH**. The protocol below starts from Helmut Schmidt's reverse-engineering work in the [dl141th](https://sourceforge.net/projects/dl141th/) project (MIT license). Every item was re-verified on a real RS-172 on 2026-11-24, partly by cross-checking against the vendor software "RH And Temp Datalogger" (`Datalogger.exe`). Items marked **(new)** were found during that verification.

## Transport

* USB-UART bridge: Silicon Labs CP210x, USB VID:PID `10C4:EA60`. On Windows it appears as COMx (driver `silabser`). On Linux it is `/dev/ttyUSBx` (driver `cp210x`, part of the mainline kernel).
* 9600 baud, 8N1, no flow control. The vendor app uses MSComm with settings `9600,n,8,1`.
* Request/response. All multi-byte values are big-endian. Dates and times are BCD.
* **(new) Pacing:** the device ignores a command that arrives immediately after its previous reply finished. A gap of 50 ms was enough; the library uses 200 ms. A chunk reply of 654 bytes takes about 1.5 s to arrive (≈ 3.5 kbit/s effective).

## Commands

| Function | Request | Reply |
|---|---|---|
| Battery | `79` | 1 byte. `04` = 70–100 % (only value observed) |
| Set clock | `58 YY MM DD hh mm ss` (BCD) | `EE` |
| Setup | `L8 FF II TH TL RH RL` | `1E` (no reply if rejected) |
| Read header | `68 68 68 68 68 68 68` | 21 bytes |
| Read data chunk | `68 68 68 68 68 68 NN` | 654 bytes (always a full chunk) |

### Set clock — `58`
Example: 2026-11-24 09:47:40 → `58 26 11 24 09 47 40`.
**(new) Side effect:** setting the clock clears the log memory and restarts the log with the current settings. With automatic start, sampling begins at the new time + about 1 s. With manual start, the log stays empty until the button is pressed. A log in progress is lost, so download before you set the clock (the vendor app does the same).

### Setup — 7 bytes, **erases the log**
| Byte | Meaning |
|---|---|
| `L8` | High nibble = LED flash cycle: `1` = 10 s, `2` = 20 s, `3` = 30 s, `4` = no light. Low nibble always `8`. The vendor ini stores this byte as `LEDDATA` (24 = 0x18, 40 = 0x28). |
| `FF` | Flags: `0x80` manual start (button) / clear = automatic (start immediately); `0x40` circular **off** / clear = circular on; `0x20` alarm-LED **off** / clear = on; `0x10` alarm thresholds in °F / clear = °C; `0x08` Tmax negative; `0x04` Tmin negative; low 2 bits = interval unit (`1` = s, `2` = min, `3` = h). |
| `II` | Interval value (1–59 s or min, 1–24 h; the vendor dialog limits each unit). |
| `TH`, `TL` | Temperature alarm high/low as an **absolute value**, with the sign in the `FF` bits (confirmed with the vendor app: −10 → `0a` + `0x04`). Unit as selected (°C or °F). |
| `RH`, `RL` | Humidity alarm high/low, 0–100 % |

**(new)** The device does not ack when TL > TH (seen with `TH=40, TL=90`).

### Header — 21 bytes
```
dd SSSS XXXX UU II ce YY MM DD hh mm ss TH TL RH RL TU SG aa
```
| Field | Meaning |
|---|---|
| `SSSS` | Number of logged bytes (4 per sample, max 65400 = 16350 samples) |
| `XXXX` | Probably related to circular overruns. 0 in every test (wraparound not tested) |
| `UU` | Interval unit: `60` = s, `30` = min, `20` = h |
| `II` | Interval value |
| `YY..ss` | Log start, BCD. This is the timestamp of sample 0 (confirmed with the vendor app) |
| `TH TL RH RL` | Alarm thresholds as set (magnitudes) |
| `TU` | `23` = °C, `24` = °F (alarm thresholds unit) |
| `SG` | `78` = both thresholds ≥ 0, `76` = TL negative, `77` = both negative |

Start mode, LED cycle, alarm LED and circular mode are **not** in the header. The vendor app only remembers them in its ini file (`%LOCALAPPDATA%\VirtualStore\Windows\HTDATALOGGER.ini`).

**Reading the header stops a running log.** Later headers report the same size. A new setup or setting the clock starts a new log.

### Data chunks
* `NN` = chunk index starting at **0**. Chunk *n* holds memory bytes `[654·n, 654·n + 654)`. Index 100 (`0x64`) wraps to chunk 0 (memory = 100 × 654 = 65400 bytes).
* Every chunk is 654 bytes, even past the logged size. Bytes beyond `SSSS` are stale data from earlier logs, so truncate to `SSSS`.
* 654 is not a multiple of 4, so samples straddle chunk boundaries. Concatenate the chunks, then decode.
* Sample = `TTTT HHHH`: int16 temperature in 0.1 °C (always °C, even when thresholds are in °F) and int16 RH in 0.1 %.
* Sample *i* timestamp = start + *i* × interval.
* The vendor app requests chunks only up to the logged size.

## Vendor "Default" button
10 s interval, LED 10 s, manual start, alarm T 0…40 °C, RH 30…90 %, alarm LED on, no circular.

## Open points
* Circular logging after wraparound: the read order and the meaning of `XXXX` are unverified.
* Battery codes other than `04`.
