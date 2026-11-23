"""功能「自动拉线与战报收集」的调度器：把「战报自动收录」插到自动拉线的空闲时间里。

间隔策略（2026-11-22 二次调整，按用户口径）：
- **硬约束：距「上一次提交成功（upload ok / 无新增）」超过 10 分钟，必须收集**；
  不是「两次收集之间不超过 10 分钟」。
- 空闲点还额外要求「距上次收集结束 >= interval」（默认 300s），避免刚收完
  又立刻开下一轮，把打架时间挤没；interval 以**结束时间**为基准。
- 每次收集完成后无论是否滑到列表底部都会尝试上传（`requireListEnd=false`，
  服务器按 BLOB 增量去重）；**上传失败的 capture 会进补交队列**，
  在后续空闲点用 `scripts/upload.py --capture-id` 重试，避免丢战报。

触发点（按优先级）：
1. `wait(seconds)`：等战斗结束的预判等待、轮间隔等「明确空闲」时间；
2. `safe_point(reason)`：本轮开始、邮件读完、据点面板关闭、战斗结束、回家补兵结束等
   世界地图上的节点边界；若已超过间隔立刻补一次。

每次收集 = 子进程执行
`skills/战报自动收录/scripts/round.py --no-wait`（采集 → 提取 → 上传，不等 /api/live），
然后无论成败都执行 `scripts/screen.py back-to-map`，把游戏恢复到世界地图，
自动拉线再继续。

状态文件：`screenshots/flow/report_collect_state.json`
（跨进程记住上次开始/结束/提交时间、补交队列）。
明细日志：`screenshots/flow/report_collect_<时间戳>.jsonl`（一条一次收集）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import vision
from .timing import fmt_seconds

# 距上次「提交成功」超过该秒数 → 无条件收集（用户口径：10 分钟）
SUBMIT_INTERVAL_SECONDS = 600.0
# 收集间隔只有下限。MAX_INTERVAL_SECONDS 仅用于状态文件/报表展示，**不做任何强制压缩**
MAX_INTERVAL_SECONDS = 24 * 3600.0
# 最小间隔（秒）：防止误把间隔调成 0 后疯狂抓包
# 2026-11-23 下调到 30s：同盟「交战」列表只保留约 6~10 分钟的滚动窗口，
# 实测约 10 张/分钟，必须每 3~5 分钟扫完一轮才能不漏（参考 list_card_team_stats）。
MIN_INTERVAL_SECONDS = 30.0
# 状态文件的有效期（秒）：太久的记录不再用来计算间隔
STATE_MAX_AGE_SECONDS = 30 * 24 * 3600


def _last_json(text: str) -> dict:
    """脚本都是「最后一行单行 JSON」；倒序找第一条可解析的。"""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return {}


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


class ReportCollector:
    """战报收集调度器（单线程、同步执行；执行期间自动拉线暂停）。"""

    def __init__(self, *, interval: float = 300.0, enabled: bool = True,
                 log=print, prepare=None, skip_upload: bool = False,
                 max_seconds: float = 1200.0, state_path: str | Path | None = None,
                 log_path: str | Path | None = None,
                 python: str | None = None,
                 skill_dir: str | Path | None = None,
                 timeout_extra: float = 600.0,
                 collect_on_start: bool = True,
                 submit_interval: float = SUBMIT_INTERVAL_SECONDS):
        self.interval = max(MIN_INTERVAL_SECONDS, float(interval))
        self.requested_interval = float(interval)
        self.submit_interval = max(MIN_INTERVAL_SECONDS, float(submit_interval))
        self.enabled = bool(enabled)
        self.log = log
        self.prepare = prepare
        self.skip_upload = bool(skip_upload)
        self.max_seconds = float(max_seconds)
        self.python = python or sys.executable
        self.skill_dir = Path(skill_dir) if skill_dir else (
            Path(__file__).resolve().parent / "skills" / "战报自动收录")
        self.timeout_extra = float(timeout_extra)
        self.state_path = Path(state_path) if state_path else Path(
            vision.shot_path("report_collect_state.json"))
        self.log_path = Path(log_path) if log_path else Path(
            vision.shot_path(f"report_collect_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"))

        self.collect_on_start = bool(collect_on_start)
        self._upload_warned = False
        self._born = time.time()
        self._last_start: float | None = None
        self._last_finish: float | None = None
        self._last_submit: float | None = None
        self._last_attempt: float | None = None   # 上次上传「尝试」（成败都算）
        self._pending: list[str] = []      # 上传失败待补交的 captureId
        self.count = 0              # 本次运行收集次数
        self.total_count = 0        # 累计（含历史状态）
        self.failures = 0
        self.gaps: list[float] = []  # 本次运行的开始间隔
        self.overdue = 0            # 距上次提交超过 10 分钟才收集的次数
        self.events: list[dict] = []
        self.last_result: dict = {}
        self._busy = False
        self._flushing = False
        self._load_state()

    # ------------------------------------------------------------ 间隔判定
    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        now = time.time()
        last = data.get("lastStart")
        if isinstance(last, (int, float)) and now - last < STATE_MAX_AGE_SECONDS:
            self._last_start = float(last)
        finish = data.get("lastFinish")
        if isinstance(finish, (int, float)) and now - finish < STATE_MAX_AGE_SECONDS:
            self._last_finish = float(finish)
        submit = data.get("lastSubmit")
        if isinstance(submit, (int, float)) and now - submit < STATE_MAX_AGE_SECONDS:
            self._last_submit = float(submit)
        attempt = data.get("lastAttempt")
        if isinstance(attempt, (int, float)) and now - attempt < STATE_MAX_AGE_SECONDS:
            self._last_attempt = float(attempt)
        pending = data.get("pendingUploads")
        if isinstance(pending, list):
            self._pending = [str(x) for x in pending if isinstance(x, str)]
        total = data.get("totalCount")
        if isinstance(total, int) and total >= 0:
            self.total_count = total

    def _save_state(self) -> None:
        payload = {
            "lastStart": self._last_start,
            "lastStartAt": _iso(self._last_start),
            "lastFinish": self._last_finish,
            "lastFinishAt": _iso(self._last_finish),
            "lastSubmit": self._last_submit,
            "lastSubmitAt": _iso(self._last_submit),
            "lastAttempt": self._last_attempt,
            "lastAttemptAt": _iso(self._last_attempt),
            "intervalSeconds": self.interval,
            "submitIntervalSeconds": self.submit_interval,
            "maxIntervalSeconds": MAX_INTERVAL_SECONDS,
            "pendingUploads": list(self._pending),
            "totalCount": self.total_count,
            "failureCount": self.failures,
            "lastOk": self.last_result.get("ok"),
            "lastCaptureId": self.last_result.get("captureId"),
            "lastError": self.last_result.get("error"),
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        except Exception as error:  # 状态文件写不了不影响主流程
            self.log(f"  ! 战报收集状态写入失败：{error!r}")

    def _submit_anchor(self) -> float | None:
        """「提交」判定基准：取「提交成功」与「提交尝试」中较新的一个。

        上传失败时 lastSubmit 不更新，但 lastAttempt 会更新——否则会按旧
        lastSubmit 每 60s 触发一次采集，形成风暴。
        """
        values = [v for v in (self._last_submit, self._last_attempt) if v]
        return max(values) if values else None

    @property
    def due(self) -> bool:
        if not self.enabled:
            return False
        now = time.time()
        # 硬约束：距上次提交（成功/尝试）超过 10 分钟 → 必须收；
        # 但 60s 内刚开过一轮就不再重开，避免采集失败时打成死循环。
        anchor = self._submit_anchor()
        recent_start = (self._last_start is not None
                        and now - self._last_start < 60.0)
        if (anchor is not None and not recent_start
                and (now - anchor) >= self.submit_interval):
            return True
        # 空闲点频次下限：距上次收集「结束」>= interval
        if self._last_finish is None:
            return (self.collect_on_start
                    or (now - self._born) >= self.interval)
        return (now - self._last_finish) >= self.interval

    def seconds_to_next(self) -> float:
        now = time.time()
        waits: list[float] = []
        anchor = self._submit_anchor()
        if anchor is not None:
            waits.append(max(0.0, self.submit_interval - (now - anchor)))
        if self._last_finish is None:
            waits.append(0.0 if self.collect_on_start
                         else max(0.0, self.interval - (now - self._born)))
        else:
            waits.append(max(0.0, self.interval - (now - self._last_finish)))
        return min(waits) if waits else 0.0

    # ------------------------------------------------------------ 触发点
    def wait(self, seconds: float) -> None:
        """分片等待；期间到点就立刻插一次战报收集。"""
        remaining_total = max(0.0, float(seconds))
        end = time.monotonic() + remaining_total
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if self._pending:
                self._flush_pending(limit=1)
            if self.due:
                self.collect(trigger="wait")
                continue
            chunk = remaining
            if self.enabled:
                # 只睡到「下一次该收集」的时刻，避免错过触发点
                chunk = min(chunk, max(0.5, self.seconds_to_next()))
            time.sleep(chunk)

    def safe_point(self, reason: str = "") -> dict | None:
        """节点边界（应在地图）：先补交失败战报，再按间隔决定是否收集。"""
        if not self.enabled or self._busy:
            return None
        self._flush_pending()
        if not self.due:
            return None
        return self.collect(trigger=reason or "safe-point")

    # ------------------------------------------------------------ 补交
    def _upload_capture(self, capture_id: str) -> bool:
        """单条补交：按 captureId 重新走 upload.py（幂等，服务器按 BLOB 去重）。"""
        script = self.skill_dir / "scripts" / "upload.py"
        if not script.is_file():
            self.log(f"  ! 找不到补交脚本：{script}")
            return False
        try:
            _code, out, _err = self._exec(
                [self.python, str(script), "--capture-id", capture_id,
                 "--no-wait"], timeout=240.0)
        except Exception as error:  # noqa: BLE001
            self.log(f"  ! 补交 {capture_id} 异常：{error!r}")
            return False
        payload = _last_json(out)
        if payload.get("ok"):
            self.log(f"  ✓ 补交成功：{capture_id}")
            return True
        reason = payload.get("error") or payload.get("reason") or "未知原因"
        self.log(f"  ! 补交失败：{capture_id}（{str(reason)[:120]}）")
        return False

    def _flush_pending(self, limit: int = 2) -> None:
        """空闲点补交：每次最多补 limit 条，避免占住太久。

        注意不能用 `_busy` 做互斥：`collect()` 内部（采集前、_busy=True）
        也要补交历史失败项，用独立 `_flushing` 防止递归即可。
        """
        if self.skip_upload or not self._pending or self._flushing:
            return
        if not self._upload_ready():
            return
        self._flushing = True
        try:
            for cid in list(self._pending[:max(1, int(limit))]):
                if self._upload_capture(cid):
                    self._pending = [p for p in self._pending if p != cid]
                    self._last_submit = time.time()
                    self._last_attempt = self._last_submit
        finally:
            self._flushing = False
            self._save_state()

    # ------------------------------------------------------------ 单次收集
    def collect(self, trigger: str = "manual") -> dict:
        if not self.enabled or self._busy:
            return {}
        self._busy = True
        started = time.time()
        gap = None
        since_submit = None
        if self._last_start is not None:
            gap = round(started - self._last_start, 1)
            self.gaps.append(gap)          # 只记录间隔，不因间隔过长告警或中断
        if self._last_submit is not None:
            since_submit = started - self._last_submit
            if since_submit >= self.submit_interval:
                self.overdue += 1
        self._last_start = started
        self.count += 1
        self.total_count += 1
        index = self.count
        self.log(f"[战报收集 #{index} {time.strftime('%H:%M:%S')}] 开始"
                 f"（触发={trigger}"
                 + (f"，距上次 {fmt_seconds(gap)}" if gap is not None else "，首次")
                 + (f"，距上次提交 {fmt_seconds(since_submit)}"
                    if since_submit is not None else "")
                 + "）")

        result: dict = {"index": index, "trigger": trigger,
                        "startedAt": _iso(started), "gapSeconds": gap,
                        "sinceSubmitSeconds": (round(since_submit, 1)
                                               if since_submit is not None else None)}
        upload_ready: bool | None = None
        try:
            if self.prepare is not None:
                try:
                    result["prepareOk"] = bool(self.prepare())
                except Exception as error:  # noqa: BLE001 - 回地图失败也继续尝试
                    result["prepareOk"] = False
                    result["prepareError"] = f"{type(error).__name__}: {error}"
                if result.get("prepareOk") is False:
                    self.log("  ! 战报收集前回到世界地图失败，仍尝试进入战报页")

            # 先补交历史失败上传：提交优先级高于新一轮采集
            self._flush_pending()

            upload_ready = self._upload_ready()
            result["uploadConfigured"] = upload_ready
            if (not upload_ready and not self.skip_upload
                    and not self._upload_warned):
                self.log("! 未找到战报上传配置（control/server_upload.json 或 "
                         "config.json 的 server.baseUrl）："
                         "本次起战报只采集到本地，不上传")
                self._upload_warned = True
            try:
                payload = self._run_round(skip_upload=not upload_ready)
            except Exception as error:  # noqa: BLE001 - 单次收集失败不终止拉线
                payload = {"ok": False, "failedStep": "run-round",
                           "error": f"{type(error).__name__}: {error}"}
            result.update(payload)

            back_ok = self._back_to_map()
            result["backToMap"] = back_ok
            if not back_ok:
                self.log("  ! 战报收集后回到世界地图失败，自动拉线可能停在战报页")
        finally:
            self._last_finish = time.time()
            result["finishedAt"] = _iso(self._last_finish)
            result["collectSeconds"] = round(self._last_finish - started, 1)
            result["ok"] = bool(result.get("ok")) and result.get("backToMap") is not False
            self._track_submit(result, upload_ready)
            if result["ok"]:
                pages = result.get("pages")
                blobs = result.get("uniqueBlobs")
                records = result.get("completeRecords")
                upload = result.get("uploadOk")
                upload_text = ("跳过" if upload is None
                               else ("ok" if upload else "失败"))
                if result.get("nothingNew"):
                    upload_text += "(无新增)"
                self.log(f"[战报收集 #{index}] 完成：页 {pages} / BLOB {blobs} / "
                         f"记录 {records} / 上传 {upload_text}"
                         f"（用时 {fmt_seconds(result['collectSeconds'])}）")
                hotspot = ((result.get("coordinateHotspot") or {}).get("hottest")
                           or {})
                if hotspot:
                    self.log(f"  [交战热点] ({hotspot.get('x')},{hotspot.get('y')}) "
                             f"{hotspot.get('count')}场 "
                             f"{float(hotspot.get('share') or 0) * 100:.1f}%")
            else:
                self.failures += 1
                detail = " ".join(str(x) for x in (
                    result.get("failedStep"), result.get("reason"),
                    result.get("error")) if x)
                self.log(f"[战报收集 #{index}] 失败：{detail or '未知原因'}")
            self.last_result = result
            self.events.append(result)
            self._append_log(result)
            self._save_state()
            self._busy = False
        return result

    def _track_submit(self, result: dict, upload_ready: bool | None) -> None:
        """维护「上次提交时间」与补交队列（不丢战报的关键）。"""
        capture_id = result.get("captureId")
        if self.skip_upload or upload_ready is False:
            # 没有上传配置/明确跳过：本地采集成功也算一次「交付」，避免 due 永远为真
            if result.get("ok"):
                self._last_submit = time.time()
                self._last_attempt = self._last_submit
            return
        if not capture_id:
            return
        if result.get("uploadOk") is True or result.get("nothingNew"):
            self._last_submit = time.time()
            self._last_attempt = self._last_submit
            self._pending = [p for p in self._pending if p != capture_id]
            return
        if result.get("uploadOk") is False:
            self._last_attempt = time.time()
            if capture_id not in self._pending:
                self._pending.append(capture_id)
                if len(self._pending) > 50:      # 上限，避免历史故障时无限增长
                    dropped = self._pending.pop(0)
                    self.log(f"  ! 补交队列超过 50 条，丢弃最旧：{dropped}")
            self.log(f"  ! 上传失败，capture 已入补交队列：{capture_id}"
                     f"（待补交 {len(self._pending)} 条）")

    # ------------------------------------------------------------ 子进程
    def _exec(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess.run(
                args, cwd=str(self.skill_dir), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=max(30.0, float(timeout)), env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"战报收集超时（>{int(timeout)}s）：{' '.join(args)}") from error
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _upload_ready(self) -> bool:
        """是否有可用的上传配置（`--report-skip-upload` 时永远 False）。"""
        if self.skip_upload:
            return False
        if (self.skill_dir / "control" / "server_upload.json").is_file():
            return True
        config_path = self.skill_dir / "config.json"
        if config_path.is_file():
            try:
                data = json.loads(config_path.read_text(encoding="utf-8-sig"))
            except Exception:
                return False
            if ((data.get("server") or {}).get("baseUrl") or "").strip():
                return True
        return False

    def _run_round(self, skip_upload: bool = False) -> dict:
        script = self.skill_dir / "scripts" / "round.py"
        if not script.is_file():
            raise FileNotFoundError(f"找不到战报收集脚本：{script}")
        args = [self.python, str(script), "--max-seconds", str(int(self.max_seconds)),
                "--no-wait", "--allow-partial"]
        if skip_upload:
            args.append("--skip-upload")
        code, out, err = self._exec(args, timeout=self.max_seconds + self.timeout_extra)
        round_result = _last_json(out)
        if not round_result:
            tail = (err or out).strip().splitlines()
            raise RuntimeError(
                f"战报收集没有输出 JSON（exit={code}）："
                f"{' | '.join(tail[-3:])[:400]}")
        captured = round_result.get("capture") or {}
        extracted = round_result.get("extract") or {}
        uploaded = round_result.get("upload") or {}
        return {
            "ok": bool(round_result.get("ok")),
            "captureId": round_result.get("captureId"),
            "coordinateHotspot": round_result.get("coordinateHotspot"),
            "pages": captured.get("pages"),
            "expandedCards": captured.get("expandedCards"),
            "captureComplete": round_result.get("captureComplete"),
            "uniqueBlobs": extracted.get("uniqueBlobs"),
            "completeRecords": extracted.get("completeRecords"),
            "uploadOk": (None if not uploaded else bool(uploaded.get("ok"))),
            "nothingNew": (None if not uploaded
                           else bool(uploaded.get("nothingNew"))),
            "ingestVersion": uploaded.get("ingestVersion"),
            "failedStep": round_result.get("failedStep"),
            "reason": round_result.get("reason"),
            "error": round_result.get("error"),
            "roundSeconds": round_result.get("seconds"),
            "summaryPath": round_result.get("summaryPath"),
        }

    def _back_to_map(self) -> bool:
        script = self.skill_dir / "scripts" / "screen.py"
        if not script.is_file():
            self.log(f"  ! 找不到回地图脚本：{script}")
            return False
        try:
            code, out, err = self._exec(
                [self.python, str(script), "back-to-map",
                 "--max-presses", "6"], timeout=180.0)
        except Exception as error:  # noqa: BLE001
            self.log(f"  ! 回地图失败：{error}")
            return False
        payload = _last_json(out)
        if payload:
            return bool(payload.get("ok"))
        self.log(f"  ! 回地图没有输出 JSON（exit={code}）："
                 f"{(err or out).strip()[-200:]}")
        return False

    # ------------------------------------------------------------ 汇总/日志
    def _append_log(self, event: dict) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as error:  # noqa: BLE001
            self.log(f"  ! 战报收集日志写入失败：{error!r}")

    def status(self) -> dict:
        gaps = list(self.gaps)
        now = time.time()
        return {
            "enabled": self.enabled,
            "intervalSeconds": self.interval,
            "submitIntervalSeconds": self.submit_interval,
            "maxIntervalSeconds": MAX_INTERVAL_SECONDS,
            "count": self.count,
            "totalCount": self.total_count,
            "failures": self.failures,
            "overdueCount": self.overdue,
            "maxGapSeconds": round(max(gaps), 1) if gaps else None,
            "avgGapSeconds": round(sum(gaps) / len(gaps), 1) if gaps else None,
            "lastStartAt": _iso(self._last_start),
            "lastFinishAt": _iso(self._last_finish),
            "lastSubmitAt": _iso(self._last_submit),
            "lastAttemptAt": _iso(self._last_attempt),
            "secondsSinceSubmit": (round(now - self._last_submit, 1)
                                   if self._last_submit else None),
            "pendingUploads": list(self._pending),
            "lastOk": self.last_result.get("ok"),
            "lastCaptureId": self.last_result.get("captureId"),
            "lastError": (None if self.last_result.get("ok")
                          else (self.last_result.get("error")
                                or self.last_result.get("reason"))),
            "statePath": str(self.state_path),
            "logPath": str(self.log_path),
        }

    def summary_line(self) -> str:
        status = self.status()
        if not status["enabled"]:
            return "已关闭"
        gap = status["maxGapSeconds"]
        return (f"收集 {status['count']} 次（失败 {status['failures']}），"
                f"最大间隔 {fmt_seconds(gap) if gap is not None else '-'}")


__all__ = ["ReportCollector", "MAX_INTERVAL_SECONDS", "MIN_INTERVAL_SECONDS",
           "SUBMIT_INTERVAL_SECONDS"]
