# Run report

Two campaigns against the simulator: one that completes, and one that halts.
The raw results are in this folder, and [instructions](instructions) explains how to reproduce them.

| File | Content |
|---|---|
| [campaign_completed.json](campaign_completed.json) | `GET /campaigns/{id}` of the first campaign, once `COMPLETED` |
| [metrics_completed.json](metrics_completed.json) | `GET /metrics` at the same moment |
| [campaign_halted.json](campaign_halted.json) | `GET /campaigns/{id}` of the second campaign, once `HALTED` |
| [metrics_halted.json](metrics_halted.json) | `GET /metrics` at the same moment |

## Setup

- **Simulator**: `DEVICE_COUNT=1000`, every other setting at its default (seed 42, default behaviour probabilities, delays and duplicates).
- **Controller**: `config.yml` as delivered (timeouts 60 s / 60 s / 600 s, 2 retries after 30 s then 60 s, tick 2 s, `min_sample_size` 20).
- Both campaigns ran one after the other on the same controller and simulator, without a restart. So the second campaign targeted only the devices still on 1.0.0, and its `/metrics` counters include the first
  campaign: the numbers below for it are the difference between the two files.
- A rehearsal run with the same settings, with the controller logs kept, gave the same counts.
Two observations below come from its logs; they are marked as such.

## Campaign 1: completed

```json
{"targetVersion": "1.1.0", "imageUrl": "https://example.com/firmware.bin",
 "deviceFilter": {"currentVersion": "1.0.0"},
 "maxConcurrent": 50, "failureThresholdPercent": 25}
```

**`COMPLETED` after 48 minutes. 848 succeeded, 152 failed: failure rate
15.2 %, under the 25 % threshold.**

| Final state | Devices |
|---|---|
| `SUCCEEDED` | 848 |
| `FAILED` | 104 |
| `TIMED_OUT` | 48 |
| **Total** | **1,000** |

| Failure reason | Devices | What happened |
|---|---|---|
| `NO_CALLBACK` (`TIMED_OUT`) | 48 | Offline: no callback after 3 Connection Requests. |
| `FAULT:9010` | 40 | Accepted the Download, then reported a TransferComplete with a fault. |
| `REJECTED:UNSUPPORTED_IMAGE` | 36 | Refused the Download. |
| `REVERTED` | 28 | Rebooted and came back on 1.0.0. About 6 of them were really faults: see below. |

No device ended `NO_ACK` or `NO_COMPLETION`: every device that got a Download reported an outcome within the timeouts.

### Key numbers from `GET /metrics`

| | Count | Comment |
|---|---|---|
| Connection Requests sent | 1,096 | 952 reachable devices × 1, plus 48 offline × 3. All accepted (202). |
| Connection Request retries | 96 | 48 offline devices × 2. |
| Downloads issued | 952 | Exactly one per device that called back. None resent. |
| Command-responses received | 1,002 | 952, plus 50 duplicates ignored. |
| Duplicate TransferCompletes ignored | 82 | |
| `CONNECTION_REQUEST` Informs | 997 | 45 more than Downloads: duplicates, which got no second Download. |
| Informs received | 13,037 | BOOT 1,094, CONNECTION_REQUEST 997, PERIODIC 10,044, TRANSFER_COMPLETE 902. |
| Errors | 0 | No 4xx, 5xx or network error, in either direction. |

The 292 `GET /campaigns/{id}` requests come from polling the campaign every 10 s.

## Campaign 2: halted

```json
{"targetVersion": "1.1.0", "imageUrl": "https://example.com/firmware.bin",
 "deviceFilter": {"currentVersion": "1.0.0"},
 "maxConcurrent": 25, "failureThresholdPercent": 5}
```

The filter matched the **152 devices left on 1.0.0** by campaign 1: the ones that failed or were offline.

**`HALTED` after 2 minutes. 34 finished, all failed: failure rate 100 %, over the 5 % threshold.**

| Final state | Devices |
|---|---|
| `FAILED` | 34 (`FAULT:9010` 14, `REVERTED` 12, `REJECTED:UNSUPPORTED_IMAGE` 8) |
| `PENDING` | 118 never started, or offline and waiting for a retry |

The halt is judged once 20 devices have finished. The devices already upgrading at that moment kept being tracked, and also failed: 34 in the end. Then no new device was started.

From the difference between the two metrics files: 54 Connection Requests (20 of them retries to offline devices), 34 Downloads, 0 errors.

## Anything surprising

1. **The threshold was almost crossed early** (rehearsal logs). Rejects come back in seconds, while a success needs a download and a reboot, so the first outcomes are mostly failures: 100 % after 2 devices. With exactly 20 devices finished, the minimum sample, the failure rate was  **25.0 %**: equal to the threshold, so not over it (the check is a strict `>`). One more early failure would have halted a healthy campaign that ended at 15.2 %. See question 5 in the README.
2. **Some faults are recorded as `REVERTED`.** These devices send their BOOT on 1.0.0 first, and their TransferComplete with `faultCode=9010` minutes later. By then the device is already `FAILED`, so the TransferComplete is ignored as late. The rehearsal logs showed this fleet has 46 faults and 22 reverts; here 40 / 28, so about 6 faults were recorded as `REVERTED`. The device counts as a failure either way; only the reason is less precise (README limitation, `TODO` in `handle_transfer_complete`).
3. **Duplicates were frequent and harmless.** 45 duplicated Connection Request Informs, 50 duplicated command-responses and 82 duplicated TransferCompletes were all recognised; none produced a second Download or changed a final result.
4. **Offline devices count as failures.** The 48 `NO_CALLBACK` devices are about a third of all failures, although they never received the firmware. Without them the failure rate would be 10.9 % (104 / 952). Whether they should count is question 1 in the README.
5. **Running again on the failed devices fails again.** Campaign 2 shows the failures are properties of the devices (the simulator assigns them per device), not bad luck: all 34 devices tried failed again, with the same kinds of failure. The halt stopped it after 34 devices instead of 152. Retrying the leftovers only makes sense after fixing the image or the devices.