"""
下载管理增强（DownloadManagerEnh）· V3 插件

目标：在下载管理下新增入口，补全 qBittorrent 各状态种子的海报与识别信息，
并支持对种子执行原生 qB Web UI 的常用操作（恢复/暂停/重新校验/删除/标签）。

原生「下载管理」(GET /api/v1/download/) 只返回正在下载的种子，做种中/已暂停/已完成
的种子根本没有统一展示入口。本插件直接拉取 qB 全量种子，按 hash 匹配 MP 整理记录
（transfer_history）补全海报/片名/来源站，并标记 MP 管理的种子（qB 标签 MOVIEPILOT）。

合规红线（moviepilot-plugin-dev）：
- 只读宿主数据：整理记录走 TransferHistoryOper.list_by_hash；种子列表走 DownloaderHelper。
- 操作种子状态通过 qB 模块实例方法（start/stop/recheck/delete/tag）下发，属用户主动触发的
  qB 操作，符合本插件定位；删除类动作一律走 POST（绝不 GET），避免浏览器预取误删。
- 不 import app.db.models；不手动改 115 / MP DB / app.env / user.db；不 kill MP。
"""
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request

try:  # 不同 MP 版本导入路径兼容
    from app.sdk.plugin import _PluginBase
except ImportError:  # pragma: no cover
    from app.plugins import _PluginBase

from app.sdk.services import DownloaderHelper
from app.sdk.logging import logger
from app.core.config import settings

LOG_TAG = "[下载管理增强] "

# 受支持的操作 -> 前端展示用中文标签
_OPERATIONS: Dict[str, str] = {
    "start": "恢复",
    "stop": "暂停",
    "recheck": "重新校验",
    "delete": "删除(保留文件)",
    "delete_files": "删除(含文件)",
    "tag": "打标签",
    "untag": "去标签",
    # 标签二级菜单（对齐 qB Web UI 的标签管理）
    "tag_menu": "标签操作",
    "tag_menu_close": "关闭标签菜单",
    "tag_apply": "打标签",
    "tag_remove": "去标签",
    "tag_create": "新建标签",
    "tag_delete": "删除标签",
}

# 需要二次确认的破坏性操作（点击第一次只登记待确认）
_CONFIRM_REQUIRED = {"delete", "delete_files", "tag_delete"}

# qB 处于「已暂停/已停止」的状态集合：这类种子显示「恢复」按钮，其余显示「暂停」。
_PAUSED_STATES = {"pausedUP", "pausedDL", "stoppedUP", "stoppedDL"}

# qB 原始状态码 → 中文（统计栏与「状态」列展示用；未收录的原样显示）
_STATE_CN: Dict[str, str] = {
    "error": "错误",
    "missingFiles": "文件缺失",
    "uploading": "上传中",
    "pausedUP": "已暂停(做种)",
    "stoppedUP": "已停止(做种)",
    "queuedUP": "排队(做种)",
    "stalledUP": "做种中",
    "checkingUP": "校验中(做种)",
    "forcedUP": "强制做种",
    "allocating": "分配空间",
    "downloading": "下载中",
    "metaDL": "获取元数据",
    "pausedDL": "已暂停(下载)",
    "stoppedDL": "已停止(下载)",
    "queuedDL": "排队(下载)",
    "stalledDL": "下载中(无活动)",
    "checkingDL": "校验中(下载)",
    "forcedDL": "强制下载",
    "moving": "移动中",
    "unknown": "未知",
}

# 删除类操作的二次确认有效期（秒）：过期后待确认记录自动失效
_PENDING_TTL = 180


class DownloadManagerEnh(_PluginBase):
    # ------------------------------------------------------------------ 元数据
    plugin_name = "下载管理增强"
    plugin_desc = "补全 qBittorrent 各状态种子的海报与识别信息，并支持对种子执行恢复/暂停/校验/删除/标签等操作。"
    plugin_icon = "downloadmanagerenh.png"
    plugin_version = "1.3.0"
    plugin_config_prefix = "downloadmanagerenh_"

    # ------------------------------------------------------------------ 配置项
    _enabled: bool = False
    _hide_downloading: bool = False
    _only_mp_tag: bool = False
    # 新建标签的候选名（逗号分隔）；qB WebUI 里是即时输入，静态页无输入框，故走配置
    _new_tags: str = "MOVIEPILOT"

    # 上一次操作结果：前端事件回调会吞掉接口返回，故在页面上以横幅反馈
    _last_result: Optional[Dict[str, Any]] = None
    # 待确认的破坏性操作：token -> {"action", "hashes", "tag", "ts"}
    _pending_deletes: Dict[str, Dict[str, Any]] = {}
    # 标签二级菜单状态：{"mode": "tag"|"untag", "hashes": [...], "row": hash|"", "ts": ts}
    _tag_menu: Optional[Dict[str, Any]] = None

    # qB 正在下载类状态：用于「隐藏下载中」时聚焦原生已覆盖的缺口
    _DOWNLOADING_STATES = {
        "downloading", "stalledDL", "queuedDL", "forcedDL",
        "checkingDL", "metaDL", "allocating", "moving",
    }
    # MP 用来标记其 qB 种子的标签
    _MP_TAG = "MOVIEPILOT"

    # ------------------------------------------------------------------ 生命周期
    def init_plugin(self, config: dict = None) -> None:
        if config:
            self._enabled = bool(config.get("enabled", False))
            self._hide_downloading = bool(config.get("hide_downloading", False))
            self._only_mp_tag = bool(config.get("only_mp_tag", False))
            # 兼容旧配置项 operate_tag（旧版本是固定标签，新版本改为新建标签候选）
            self._new_tags = str(
                config.get("new_tags") or config.get("operate_tag") or "MOVIEPILOT"
            ).strip()

    def get_state(self) -> bool:
        return bool(self._enabled)

    # ------------------------------------------------------------------ 抽象方法补齐（_PluginBase 契约）
    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/operate",
            "endpoint": self.api_operate,
            "methods": ["POST"],
            "auth": "apikey",
            "summary": "对种子执行操作（恢复/暂停/校验/删除/标签）",
            "description": "接收 action 与 hashes，按各下载器分发批量操作；删除类动作仅限 POST。",
        }]

    def stop_service(self) -> None:
        """无后台调度，无需清理。"""
        return None

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 4, self._switch("enabled", "启用插件",
                                "开启后新增下载管理入口，补全各状态种子海报与识别信息")),
                            self._col(12, 4, self._switch("hide_downloading", "隐藏下载中的种子",
                                "原生已展示下载中，开启后聚焦做种/暂停/已完成等缺口")),
                            self._col(12, 4, self._switch("only_mp_tag", "仅显示 MP 管理的种子",
                                "只展示带 MOVIEPILOT 标签、由 MP 接管的种子")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 6, {
                                "component": "VTextField",
                                "props": {
                                    "model": "new_tags",
                                    "label": "新建标签的候选名（英文逗号分隔多个）",
                                    "hint": "qB WebUI 里标签是即时输入新建的，本插件页面无输入框，"
                                            "故把常用标签名填在这里：点「打标签」→「新建标签」即可创建并打上。"
                                            "MP 默认用 MOVIEPILOT 标记自己接管的种子",
                                    "persistent-hint": True,
                                },
                            }),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "hide_downloading": False,
            "only_mp_tag": False,
            "new_tags": "MOVIEPILOT",
        }

    @staticmethod
    def _col(cols: int, md: int, content: dict) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": cols, "md": md},
            "content": [content],
        }

    @staticmethod
    def _switch(model: str, label: str, hint: str) -> dict:
        return {
            "component": "VSwitch",
            "props": {"model": model, "label": label, "hint": hint, "persistent-hint": True},
        }

    # ------------------------------------------------------------------ 入口页面
    def get_page(self) -> Optional[List[dict]]:
        if not self._enabled:
            return None
        rows, state_counts = self._build_rows()
        if not rows:
            return [{
                "component": "div",
                "text": "未从 qBittorrent 获取到任何种子，请确认下载器已连接且本插件已启用。",
                "props": {"class": "text-center"},
            }]
        return self._render(rows, state_counts)

    # ------------------------------------------------------------------ 下载器
    @staticmethod
    def _get_qb_services() -> Dict[str, Any]:
        helper = DownloaderHelper()
        services = helper.get_services()
        if not services:
            logger.warning(f"{LOG_TAG}未获取到任何下载器实例")
            return {}
        result: Dict[str, Any] = {}
        for name, info in services.items():
            try:
                if not helper.is_downloader(service_type="qbittorrent", service=info):
                    continue
            except Exception as err:  # pragma: no cover
                logger.warning(f"{LOG_TAG}判断下载器类型失败 [{name}]：{err}")
                continue
            if not getattr(info, "instance", None):
                continue
            try:
                if info.instance.is_inactive():
                    continue
            except Exception:  # pragma: no cover
                continue
            result[name] = info
        return result

    # ------------------------------------------------------------------ qB 种子字段（dict）
    @staticmethod
    def _t_hash(t: Any) -> str:
        return str(t.get("hash") or "").strip() if isinstance(t, dict) else ""

    @staticmethod
    def _t_name(t: Any) -> str:
        return str(t.get("name") or "") if isinstance(t, dict) else ""

    @staticmethod
    def _t_state(t: Any) -> str:
        return str(t.get("state") or "").strip() if isinstance(t, dict) else ""

    @staticmethod
    def _t_tags(t: Any) -> List[str]:
        if not isinstance(t, dict):
            return []
        return [s.strip() for s in str(t.get("tags") or "").split(",") if s.strip()]

    @staticmethod
    def _t_ratio(t: Any) -> float:
        if not isinstance(t, dict):
            return 0.0
        try:
            return float(t.get("ratio") or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _t_seeding_time(t: Any) -> float:
        if not isinstance(t, dict):
            return 0.0
        try:
            return float(t.get("seeding_time") or t.get("time_active") or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _t_size(t: Any) -> float:
        if not isinstance(t, dict):
            return 0.0
        try:
            return float(t.get("size") or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _t_progress(t: Any) -> float:
        if not isinstance(t, dict):
            return 0.0
        try:
            p = float(t.get("progress") or 0)
        except (TypeError, ValueError):
            return 0.0
        return p * 100 if p <= 1 else p

    # ------------------------------------------------------------------ 整理记录
    @staticmethod
    def _batch_history(hashes: List[str]) -> Dict[str, Dict[str, Any]]:
        """按 hash 批量查整理记录，补全海报/片名/来源站（只读 TransferHistoryOper）。"""
        hashes = [h for h in dict.fromkeys(hashes) if h]
        if not hashes:
            return {}
        try:
            from app.db.oper.transferhistory import TransferHistoryOper
            oper = TransferHistoryOper()
            out: Dict[str, Dict[str, Any]] = {}
            for h in hashes:
                rows = oper.list_by_hash(h) or []
                for rec in rows:
                    out[h] = {
                        "image": getattr(rec, "image", None),
                        "title": getattr(rec, "title", None),
                        "type": getattr(rec, "type", None),
                        "seasons": getattr(rec, "seasons", None),
                        "episodes": getattr(rec, "episodes", None),
                        "year": getattr(rec, "year", None),
                        "site": getattr(rec, "torrent_site", None)
                        or getattr(rec, "site_name", None),
                        "status": bool(getattr(rec, "status", None)),
                    }
                    break  # 取第一条即可
            return out
        except Exception as err:  # pragma: no cover
            logger.warning(f"{LOG_TAG}查询整理记录失败：{err}")
            return {}

    # ------------------------------------------------------------------ 数据组装
    def _build_rows(self):
        services = self._get_qb_services()
        if not services:
            return [], {}
        all_torrents: List[Any] = []
        for svc_name, svc in services.items():
            try:
                result = svc.instance.get_torrents()
            except Exception as err:  # pragma: no cover
                logger.warning(f"{LOG_TAG}获取下载器[{svc_name}]种子失败：{err}")
                continue
            # Qbittorrent.get_torrents() 返回 (种子列表, 错误标志) 二元组，必须拆包；
            if isinstance(result, tuple) and len(result) == 2:
                torrents, error = result
            else:
                torrents, error = (result or []), False
            if error:
                logger.warning(f"{LOG_TAG}下载器[{svc_name}]未连接或返回错误，已跳过")
                continue
            all_torrents.extend(torrents or [])
        if not all_torrents:
            return [], {}

        hashes = [self._t_hash(t) for t in all_torrents]
        history = self._batch_history(hashes)

        rows: List[Dict[str, Any]] = []
        state_counts: Dict[str, int] = {}
        for t in all_torrents:
            h = self._t_hash(t)
            if not h:
                continue
            state = self._t_state(t)
            tags = self._t_tags(t)
            is_mp = self._MP_TAG in [x.upper() for x in tags]
            # 过滤：隐藏下载中（原生已展示）
            if self._hide_downloading and (state in self._DOWNLOADING_STATES or "DL" in state):
                continue
            # 过滤：仅 MP 管理种子
            if self._only_mp_tag and not is_mp:
                continue
            rec = history.get(h) or {}
            poster = rec.get("image")
            title = rec.get("title") or self._t_name(t)
            rows.append({
                "hash": h,
                "poster": poster,
                "title": title,
                "state": state,
                "progress": self._t_progress(t),
                "size": self._fmt_size(self._t_size(t)),
                "size_bytes": self._t_size(t),
                "ratio": self._t_ratio(t),
                "seeding": self._fmt_duration(self._t_seeding_time(t)),
                "tags": ", ".join(tags) if tags else "",
                "site": rec.get("site") or "",
                "is_mp": is_mp,
            })
            state_counts[state] = state_counts.get(state, 0) + 1

        # 排序：做种/暂停优先，下载中靠后
        rows.sort(key=lambda r: (
            0 if ("UP" in r["state"] or "uploading" in r["state"]) else 1,
            r["state"], r["title"],
        ))
        return rows, state_counts

    # ------------------------------------------------------------------ 格式化
    @staticmethod
    def _state_cn(state: str) -> str:
        """qB 状态码转中文；未收录的原样返回。"""
        key = (state or "").strip()
        return _STATE_CN.get(key, key or "未知")

    @staticmethod
    def _fmt_size(size: float) -> str:
        if size >= 1e9:
            return f"{size / 1e9:.2f} GB"
        if size >= 1e6:
            return f"{size / 1e6:.0f} MB"
        if size >= 1e3:
            return f"{size / 1e3:.0f} KB"
        return f"{size:.0f} B"

    @staticmethod
    def _fmt_duration(sec: float) -> str:
        sec = int(sec)
        if sec <= 0:
            return "-"
        d, rem = divmod(sec, 86400)
        h, rem = divmod(rem, 3600)
        m, _ = divmod(rem, 60)
        if d:
            return f"{d}天{h}时"
        if h:
            return f"{h}时{m}分"
        return f"{m}分"

    # ------------------------------------------------------------------ 操作端点
    async def api_operate(self, request: Request):
        """
        POST /api/v1/plugin/DownloadManagerEnh/operate
        请求体：{"action": str, "hashes": [str], "tag": str(可选), "confirm": str(可选),
                 "mode": "tag"|"untag"(可选), "row": str(可选)}
        删除类动作只接受 POST，避免浏览器预取/误点触发误删；
        删除/删标签走「两段式确认」：第一次仅登记待确认，第二次带 confirm token 才真正执行。
        """
        try:
            body = await request.json()
        except Exception:
            return self._finish(False, "请求体解析失败，请确认使用 JSON 提交")
        if not isinstance(body, dict):
            return self._finish(False, "请求体格式错误")
        action = str(body.get("action") or "").strip()
        hashes = body.get("hashes") or []
        tag = str(body.get("tag") or "").strip()
        confirm = str(body.get("confirm") or "").strip()
        mode = str(body.get("mode") or "").strip()
        row = str(body.get("row") or "").strip()

        # 清除结果横幅
        if action == "clear_result":
            self._last_result = None
            return {"success": True, "message": "已清除提示"}

        # 标签二级菜单：打开 / 关闭
        if action == "tag_menu":
            if mode not in ("tag", "untag"):
                return self._finish(False, "未指定标签操作类型")
            if not isinstance(hashes, list) or not hashes:
                return self._finish(False, "未提供种子哈希")
            self._tag_menu = {
                "mode": mode,
                "hashes": [str(h).strip().lower() for h in hashes if str(h).strip()],
                "row": row,
                "ts": time.time(),
            }
            tip = "为选中种子添加标签（可选已有标签，或新建一个）" if mode == "tag" \
                else "从选中种子移除标签，或彻底删除某个标签"
            return self._finish(True, tip)
        if action == "tag_menu_close":
            self._tag_menu = None
            return self._finish(True, "已关闭标签菜单")

        if action not in _OPERATIONS:
            return self._finish(False, f"不支持的操作：{action}")

        # 标签类：应用已有标签 / 新建标签 / 移除标签 / 删除标签定义
        if action in ("tag_apply", "tag_remove", "tag_create"):
            if not tag:
                return self._finish(False, "未指定标签名")
            if not isinstance(hashes, list) or not hashes:
                return self._finish(False, "未提供种子哈希")
            hashes = [str(h).strip().lower() for h in hashes if str(h).strip()]
            if not hashes:
                return self._finish(False, "未提供有效种子哈希")
            if action == "tag_apply":
                ok_count, fail_count = self._do_operate("tag", hashes, tag)
            elif action == "tag_remove":
                ok_count, fail_count = self._do_operate("untag", hashes, tag)
            else:
                ok_count, fail_count = self._do_tag_create(tag, hashes)
            if ok_count == 0:
                return self._finish(False, f"操作未生效（{fail_count} 个种子），请确认下载器已连接且种子仍存在")
            msg = f"已对 {ok_count} 个种子执行「{_OPERATIONS[action]}：{tag}」"
            if fail_count:
                msg += f"，{fail_count} 个跳过/失败"
            return self._finish(True, msg)

        if action == "tag_delete":
            if not tag:
                return self._finish(False, "未指定要删除的标签")
            if not confirm:
                token = self._stage_delete("tag_delete", [], tag=tag)
                return self._finish(
                    True,
                    f"删除标签「{tag}」会影响 qB 中所有带该标签的种子，请再次点击「确认删除标签」执行；本次未删除",
                    extra={"confirm": token},
                )
            staged = self._pending_deletes.get(confirm)
            if not staged or staged.get("action") != "tag_delete" or staged.get("tag") != tag:
                return self._finish(False, "确认已失效（可能已超时），请重新点击删除标签")
            if time.time() - float(staged.get("ts") or 0) > _PENDING_TTL:
                self._pending_deletes.pop(confirm, None)
                return self._finish(False, f"确认已超过 {_PENDING_TTL} 秒未确认，已自动作废，请重新点击")
            self._pending_deletes.pop(confirm, None)
            ok = self._do_tag_delete(tag)
            return self._finish(bool(ok), f"已删除标签「{tag}」" if ok else f"删除标签「{tag}」失败")

        if not isinstance(hashes, list) or not hashes:
            return self._finish(False, "未提供种子哈希")
        hashes = [str(h).strip().lower() for h in hashes if str(h).strip()]
        if not hashes:
            return self._finish(False, "未提供有效种子哈希")

        # 删除类：两段式确认（对齐 qB Web UI 的删除确认，杜绝「点一下就删」）
        if action in ("delete", "delete_files"):
            if not confirm:
                token = self._stage_delete(action, hashes)
                return self._finish(
                    True,
                    f"已选中 {len(hashes)} 个种子，请再次点击「确认{_OPERATIONS[action]}」按钮执行；本次未删除任何内容",
                    extra={"confirm": token},
                )
            staged = self._pending_deletes.get(confirm)
            if not staged or staged.get("action") != action:
                return self._finish(False, "确认已失效（可能已超时），请重新点击删除按钮")
            if time.time() - float(staged.get("ts") or 0) > _PENDING_TTL:
                # 过期确认一律作废，绝不执行删除
                self._pending_deletes.pop(confirm, None)
                return self._finish(False, f"确认已超过 {_PENDING_TTL} 秒未确认，已自动作废，请重新点击删除按钮")
            self._pending_deletes.pop(confirm, None)
            hashes = [str(h).lower() for h in (staged.get("hashes") or [])] or hashes

        ok_count, fail_count = self._do_operate(action, hashes, tag)
        if ok_count == 0:
            return self._finish(False, f"操作未生效（{fail_count} 个种子），请确认下载器已连接且种子仍存在")
        msg = f"已对 {ok_count} 个种子执行「{_OPERATIONS[action]}」"
        if fail_count:
            msg += f"，{fail_count} 个跳过/失败"
        return self._finish(True, msg)

    # ------------------------------------------------------------------ 操作反馈
    def _finish(self, success: bool, message: str,
                extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """记录并返回操作结果；前端事件回调不展示返回体，故由页面横幅呈现。"""
        self._last_result = {"success": bool(success), "message": message, "ts": time.time()}
        res: Dict[str, Any] = {"success": bool(success), "message": message}
        if extra:
            res.update(extra)
        return res

    def _stage_delete(self, action: str, hashes: List[str], tag: str = "") -> str:
        """登记一次待确认的破坏性操作，返回确认 token。"""
        now = time.time()
        for token in [t for t, v in self._pending_deletes.items()
                      if now - float(v.get("ts") or 0) > _PENDING_TTL]:
            self._pending_deletes.pop(token, None)
        token = uuid.uuid4().hex[:12]
        self._pending_deletes[token] = {
            "action": action, "hashes": list(hashes), "tag": tag, "ts": now}
        return token

    def _pending_token(self, action: str, hashes: List[str], tag: str = "") -> Optional[str]:
        """查找与「操作 + 种子集合 + 标签」完全一致的待确认 token。"""
        now = time.time()
        wanted = set(hashes)
        for token in list(self._pending_deletes.keys()):
            staged = self._pending_deletes.get(token) or {}
            if now - float(staged.get("ts") or 0) > _PENDING_TTL:
                self._pending_deletes.pop(token, None)
                continue
            if staged.get("action") != action:
                continue
            if (staged.get("tag") or "") != (tag or ""):
                continue
            if set(staged.get("hashes") or []) == wanted:
                return token
        return None

    # ------------------------------------------------------------------ 标签管理
    def _get_all_tags(self) -> List[str]:
        """列出 qB 中现存的全部标签（/api/v2/torrents/tags）。"""
        tags = set()
        for svc_name, svc in self._get_qb_services().items():
            qbc = getattr(svc.instance, "qbc", None)
            if not qbc:
                continue
            try:
                raw = qbc.torrents_tags()
            except Exception as err:  # pragma: no cover
                logger.warning(f"{LOG_TAG}获取下载器[{svc_name}]标签列表失败：{err}")
                continue
            items = raw if isinstance(raw, (list, tuple, set)) else str(raw or "").split(",")
            for item in items:
                name = str(item).strip()
                if name:
                    tags.add(name)
        return sorted(tags)

    def _new_tag_candidates(self) -> List[str]:
        return [t.strip() for t in str(self._new_tags or "").split(",") if t.strip()]

    def _do_tag_create(self, tag: str, hashes: List[str]) -> Tuple[int, int]:
        """新建标签（/torrents/createTags）并给指定种子打上（/torrents/addTags）。"""
        ok_total = 0
        fail_total = 0
        for svc_name, svc, target in self._group_by_service(hashes):
            qbc = getattr(svc.instance, "qbc", None)
            if not qbc:
                fail_total += len(target)
                continue
            try:
                qbc.torrents_create_tags(tags=tag)
            except Exception as err:
                logger.error(f"{LOG_TAG}下载器[{svc_name}]新建标签[{tag}]失败：{err}")
                fail_total += len(target)
                continue
            try:
                res = svc.instance.set_torrents_tag(target, [tag])
            except Exception as err:
                logger.error(f"{LOG_TAG}下载器[{svc_name}]新建后打标签失败：{err}")
                res = False
            if res:
                ok_total += len(target)
            else:
                fail_total += len(target)
        return ok_total, fail_total

    def _do_tag_delete(self, tag: str) -> bool:
        """彻底删除标签定义（/torrents/deleteTags），该标签会从所有种子上消失。"""
        done = False
        for svc_name, svc in self._get_qb_services().items():
            qbc = getattr(svc.instance, "qbc", None)
            if not qbc:
                continue
            try:
                qbc.torrents_delete_tags(tags=tag)
                done = True
            except Exception as err:
                logger.error(f"{LOG_TAG}下载器[{svc_name}]删除标签[{tag}]失败：{err}")
        return done

    def _group_by_service(self, hashes: List[str]) -> List[Tuple[str, Any, List[str]]]:
        """把目标 hash 按所属下载器分组，只把该下载器自己的种子交给它。"""
        services = self._get_qb_services()
        if not services:
            return []
        wanted = set(hashes)
        groups: List[Tuple[str, Any, List[str]]] = []
        for svc_name, svc in services.items():
            try:
                result = svc.instance.get_torrents()
            except Exception as err:  # pragma: no cover
                logger.warning(f"{LOG_TAG}操作前获取下载器[{svc_name}]种子失败：{err}")
                continue
            if isinstance(result, tuple) and len(result) == 2:
                torrents, error = result
            else:
                torrents, error = (result or []), False
            if error or not torrents:
                continue
            local_set = {self._t_hash(t).lower() for t in torrents if self._t_hash(t)}
            target = [h for h in hashes if h in local_set]
            if target:
                groups.append((svc_name, svc, target))
        return groups

    def _do_operate(self, action: str, hashes: List[str], tag: str) -> Tuple[int, int]:
        """
        按下载器分发操作。每个下载器只对自己的种子执行，避免把外来 hash 传给别的实例。
        返回 (成功计数, 失败/跳过计数)。
        """
        groups = self._group_by_service(hashes)
        if not groups:
            return 0, len(hashes)
        serviced: set = set()
        ok_total = 0
        fail_total = 0
        for svc_name, svc, target in groups:
            try:
                if action == "start":
                    res = svc.instance.start_torrents(target)
                elif action == "stop":
                    res = svc.instance.stop_torrents(target)
                elif action == "recheck":
                    res = svc.instance.recheck_torrents(target)
                elif action == "delete":
                    res = svc.instance.delete_torrents(False, target)
                elif action == "delete_files":
                    res = svc.instance.delete_torrents(True, target)
                elif action == "tag":
                    res = svc.instance.set_torrents_tag(target, [tag]) if tag else False
                elif action == "untag":
                    res = svc.instance.remove_torrents_tag(target, tag) if tag else False
                else:
                    res = False
            except Exception as err:
                logger.error(f"{LOG_TAG}下载器[{svc_name}]执行[{action}]出错：{err}")
                res = False
            serviced.update(target)
            if res:
                ok_total += len(target)
            else:
                fail_total += len(target)
        unmatched = set(hashes) - serviced
        fail_total += len(unmatched)
        return ok_total, fail_total

    # ------------------------------------------------------------------ 渲染
    def _render(self, rows: List[Dict[str, Any]], state_counts: Dict[str, int]) -> List[dict]:
        all_hashes = [r["hash"] for r in rows]
        cards = [self._stat_card(str(len(rows)), "合计种子")]
        for st, c in sorted(state_counts.items(), key=lambda x: -x[1]):
            cards.append(self._stat_card(str(c), self._state_cn(st)))
        total_bytes = sum(float(r.get("size_bytes") or 0) for r in rows)
        cards.append(self._stat_card(self._fmt_size(total_bytes), "占用空间"))
        header = {"component": "VRow", "content": cards}
        toolbar = self._batch_toolbar(all_hashes)

        # 桌面表格：中等屏及以上显示（hidden-sm-and-down）
        thead = {
            "component": "thead",
            "content": [
                self._th("海报"), self._th("名称/片名"), self._th("状态"),
                self._th("进度"), self._th("大小"), self._th("分享率"),
                self._th("做种时长"), self._th("标签"),
                self._th("操作"),
            ],
        }
        trs = [self._row(r) for r in rows]
        table_desktop = {
            "component": "VCol",
            "props": {"cols": 12, "class": "hidden-sm-and-down"},
            # 外层横向滚动 + 表格最小宽度：避免「操作」列被对话框右边缘截断
            "content": [{
                "component": "div",
                "props": {"style": "overflow-x:auto"},
                "content": [{
                    "component": "VTable",
                    "props": {"hover": True, "density": "compact", "style": "min-width:920px"},
                    "content": [thead, {"component": "tbody", "content": trs}],
                }],
            }],
        }

        # 手机卡片：小屏显示（hidden-md-and-up）；底部留白避免被 MP 悬浮助手按钮遮挡
        mobile = {
            "component": "VCol",
            "props": {"cols": 12, "class": "hidden-md-and-up pb-12"},
            "content": [self._row_mobile(r) for r in rows],
        }

        out: List[dict] = []
        banner = self._result_banner()
        if banner:
            out.append(banner)
        out.append(header)
        if toolbar:
            out.append(toolbar)
        out.extend([table_desktop, mobile])
        return out

    @staticmethod
    def _th(text: str) -> dict:
        return {"component": "th", "props": {"class": "text-start ps-4 text-no-wrap"}, "text": text}

    @staticmethod
    def _stat_card(value: str, label: str) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 6, "sm": 4, "md": 2},
            "content": [{
                "component": "VCard",
                "props": {"class": "text-center pa-2"},
                "content": [
                    {"component": "VCardText", "props": {"class": "text-h6 text-no-wrap"}, "text": value},
                    {"component": "VCardText", "props": {"class": "text-caption text-no-wrap"}, "text": label},
                ],
            }],
        }

    # ------------------------------------------------------------------ 结果横幅
    def _result_banner(self) -> Optional[dict]:
        """展示上一次操作结果（前端事件回调不弹返回体，故在此反馈）。"""
        result = self._last_result
        if not result:
            return None
        ok = bool(result.get("success"))
        color = "text-success" if ok else "text-error"
        return {
            "component": "VCard",
            "props": {"class": "pa-2 mb-3", "style": "border-left:3px solid currentColor"},
            "content": [{
                "component": "div",
                "props": {"class": f"d-flex align-center {color}"},
                "content": [
                    {"component": "span", "props": {"class": "text-body-2"},
                     "text": ("✔ " if ok else "✖ ") + str(result.get("message") or "")},
                    {"component": "div", "props": {"class": "flex-grow-1"}},
                    {
                        "component": "VBtn",
                        "props": {"size": "x-small", "variant": "text"},
                        "text": "知道了",
                        "events": {"click": {
                            "api": f"plugin/DownloadManagerEnh/operate?apikey={settings.API_TOKEN}",
                            "method": "post",
                            "params": {"action": "clear_result", "hashes": []},
                        }},
                    },
                ],
            }],
        }

    # ------------------------------------------------------------------ 操作按钮
    @staticmethod
    def _act_btn(label: str, action: str, hashes: List[str], color: str,
                 tag: Optional[str] = None, confirm: Optional[str] = None,
                 variant: str = "text", extra: Optional[Dict[str, Any]] = None) -> dict:
        params: Dict[str, Any] = {"action": action, "hashes": hashes}
        if tag:
            params["tag"] = tag
        if confirm:
            params["confirm"] = confirm
        if extra:
            params.update(extra)
        return {
            "component": "VBtn",
            "props": {
                "size": "x-small",
                "variant": variant,
                "color": color,
                # 行内按钮用紧凑间距（text 变体），批量栏用 tonal 突出
                "class": "ma-1 text-no-wrap" if variant == "tonal" else "mr-1 my-1 text-no-wrap",
            },
            "text": label,
            "events": {
                "click": {
                    "api": f"plugin/DownloadManagerEnh/operate?apikey={settings.API_TOKEN}",
                    "method": "post",
                    "params": params,
                }
            },
        }

    def _action_buttons(self, r: Dict[str, Any]) -> List[dict]:
        h = r["hash"]
        is_paused = r["state"] in _PAUSED_STATES
        btns: List[dict] = []
        if is_paused:
            btns.append(self._act_btn("恢复", "start", [h], "success"))
        else:
            btns.append(self._act_btn("暂停", "stop", [h], "warning"))
        btns.append(self._act_btn("校验", "recheck", [h], "info"))
        # 删除类：已登记待确认时，按钮变为「确认删除」，点击才真正执行
        pending_del = self._pending_token("delete", [h])
        pending_del_files = self._pending_token("delete_files", [h])
        if pending_del:
            btns.append(self._act_btn("确认删除", "delete", [h], "error", confirm=pending_del))
        else:
            btns.append(self._act_btn("删除", "delete", [h], "error"))
        if pending_del_files:
            btns.append(self._act_btn("确认删文件", "delete_files", [h], "error", confirm=pending_del_files))
        else:
            btns.append(self._act_btn("删文件", "delete_files", [h], "error"))
        # 标签：一级按钮只负责展开二级菜单（选已有标签 / 新建标签 / 删除标签）
        btns.append(self._act_btn("打标签", "tag_menu", [h], "primary",
                                  extra={"mode": "tag", "row": h}))
        btns.append(self._act_btn("去标签", "tag_menu", [h], "secondary",
                                  extra={"mode": "untag", "row": h}))
        return btns

    # ------------------------------------------------------------------ 标签二级菜单
    def _tag_panel(self, mode: str, hashes: List[str],
                   row: Optional[Dict[str, Any]] = None) -> dict:
        """标签二级菜单：打标签 = 选已有标签 / 新建标签；去标签 = 从种子移除 / 删除标签定义。"""
        all_tags = self._get_all_tags()
        blocks: List[dict] = []

        if mode == "tag":
            blocks.append({"component": "div", "props": {"class": "text-caption font-weight-bold"},
                           "text": "① 选择已有标签（直接给选中种子打上）"})
            if all_tags:
                blocks.append({"component": "div", "props": {"class": "d-flex flex-wrap"},
                               "content": [self._act_btn(t, "tag_apply", hashes, "primary", tag=t)
                                           for t in all_tags]})
            else:
                blocks.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                               "text": "qB 中还没有任何标签"})
            blocks.append({"component": "div", "props": {"class": "text-caption font-weight-bold mt-2"},
                           "text": "② 新建标签（qB 中创建后自动打上）"})
            candidates = self._new_tag_candidates()
            if candidates:
                blocks.append({"component": "div", "props": {"class": "d-flex flex-wrap"},
                               "content": [self._act_btn(f"＋{t}", "tag_create", hashes, "success", tag=t)
                                           for t in candidates]})
                blocks.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                               "text": "（新建标签名可在插件设置里配置，多个用英文逗号分隔）"})
            else:
                blocks.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                               "text": "未配置新建标签名，请到插件设置里填写"})
        else:
            cur = [t.strip() for t in str((row or {}).get("tags") or "").split(",") if t.strip()]
            blocks.append({"component": "div", "props": {"class": "text-caption font-weight-bold"},
                           "text": "① 从选中种子上移除标签"})
            if cur:
                blocks.append({"component": "div", "props": {"class": "d-flex flex-wrap"},
                               "content": [self._act_btn(f"－{t}", "tag_remove", hashes, "warning", tag=t)
                                           for t in cur]})
            else:
                blocks.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                               "text": "选中种子当前没有标签"})
            blocks.append({"component": "div", "props": {"class": "text-caption font-weight-bold mt-2"},
                           "text": "② 删除标签定义（qB 中彻底删除，所有带该标签的种子都会受影响）"})
            if all_tags:
                del_btns = []
                for t in all_tags:
                    pending = self._pending_token("tag_delete", [], tag=t)
                    if pending:
                        del_btns.append(self._act_btn(f"确认删除标签「{t}」", "tag_delete", [], "error",
                                                      tag=t, confirm=pending))
                    else:
                        del_btns.append(self._act_btn(f"✕ {t}", "tag_delete", [], "error", tag=t))
                blocks.append({"component": "div", "props": {"class": "d-flex flex-wrap"},
                               "content": del_btns})
            else:
                blocks.append({"component": "div", "props": {"class": "text-caption text-medium-emphasis"},
                               "text": "qB 中还没有任何标签"})

        blocks.append({"component": "div", "props": {"class": "d-flex flex-wrap mt-2"},
                       "content": [self._act_btn("关闭", "tag_menu_close", [], "secondary")]})
        return {
            "component": "div",
            "props": {"class": "pa-2 mt-1",
                      "style": "border:1px dashed rgba(255,255,255,.3);border-radius:6px;"
                               "background:rgba(255,255,255,.05);min-width:280px;"},
            "content": blocks,
        }

    def _tag_panel_for(self, hashes: List[str], row_hash: str = "",
                       row: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        """当前二级菜单是否针对该目标（行内或批量）。"""
        menu = self._tag_menu
        if not menu:
            return None
        if (menu.get("row") or "") != (row_hash or ""):
            return None
        return self._tag_panel(menu.get("mode") or "tag", menu.get("hashes") or hashes, row)

    def _batch_toolbar(self, all_hashes: List[str]) -> Optional[dict]:
        if not all_hashes:
            return None
        pending_del = self._pending_token("delete", all_hashes)
        pending_del_files = self._pending_token("delete_files", all_hashes)
        btns = [
            self._act_btn("全部恢复", "start", all_hashes, "success", variant="tonal"),
            self._act_btn("全部暂停", "stop", all_hashes, "warning", variant="tonal"),
            self._act_btn("全部校验", "recheck", all_hashes, "info", variant="tonal"),
            self._act_btn("确认全部删除(留文件)" if pending_del else "全部删除(留文件)",
                          "delete", all_hashes, "error", variant="tonal", confirm=pending_del),
            self._act_btn("确认全部删除(含文件)" if pending_del_files else "全部删除(含文件)",
                          "delete_files", all_hashes, "error", variant="tonal", confirm=pending_del_files),
            self._act_btn("批量打标签", "tag_menu", all_hashes, "primary", variant="tonal",
                          extra={"mode": "tag", "row": ""}),
            self._act_btn("批量去标签", "tag_menu", all_hashes, "secondary", variant="tonal",
                          extra={"mode": "untag", "row": ""}),
        ]
        content: List[dict] = [
            {"component": "VCardText", "props": {"class": "text-subtitle-1 pb-1"},
             "text": "批量操作（作用于当前可见的全部种子）"},
            {"component": "div", "props": {"class": "d-flex flex-wrap"}, "content": btns},
            {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"},
             "text": "说明：删除类按钮需点两次确认；「批量打标签 / 批量去标签」会展开二级菜单，"
                     "可选择 qB 已有标签、新建标签，或从种子移除 / 删除标签定义。"},
        ]
        panel = self._tag_panel_for(all_hashes, "")
        if panel:
            content.append(panel)
        return {
            "component": "VCard",
            "props": {"class": "pa-2 mb-3"},
            "content": content,
        }

    # ------------------------------------------------------------------ 行渲染
    @staticmethod
    def _poster_box(size: int) -> dict:
        """无海报时的等尺寸占位块，保证列宽稳定不跳动。"""
        return {
            "component": "div",
            "props": {"style": f"width:{size}px;height:{int(size * 1.5)}px;border-radius:4px;"
                               "background:rgba(255,255,255,.08);"},
            "text": "",
        }

    @classmethod
    def _poster_img(cls, r: Dict[str, Any], size: int = 48) -> dict:
        """
        用 Vuetify 的 VImg 渲染海报（与 MP 原生下载页一致：固定尺寸 + cover 裁剪 +
        自带占位/懒加载），避免裸 img 在窄列里被压成细条。
        eager=True：VImg 默认懒加载，表格里行未进视口就不会出图，必须强制加载。
        """
        if not r["poster"]:
            return cls._poster_box(size)
        return {
            "component": "VImg",
            "props": {
                "src": r["poster"],
                "width": size,
                "height": int(size * 1.5),
                "aspect-ratio": "2/3",
                "cover": True,
                "eager": True,
                "class": "rounded",
            },
        }

    @classmethod
    def _poster_td(cls, r: Dict[str, Any]) -> dict:
        return {
            "component": "td",
            # 固定列宽：防止表格布局把海报列挤扁
            "props": {"style": "width:60px;min-width:60px"},
            "content": [cls._poster_img(r, 48)],
        }

    def _row(self, r: Dict[str, Any]) -> dict:
        mp_badge = "是" if r["is_mp"] else "否"
        caption_cls = "text-success" if r["is_mp"] else "text-medium-emphasis"
        return {
            "component": "tr",
            "props": {"class": "text-sm"},
            "content": [
                self._poster_td(r),
                # 名称列合并「来源站 / MP管理」，为「操作」列让出宽度
                {"component": "td", "content": [
                    {"component": "div", "props": {"class": "text-body-2"},
                     "text": r["title"]},
                    {"component": "div", "props": {"class": "text-caption " + caption_cls},
                     "text": f"{r['site'] or '—'} · MP管理：{mp_badge}"},
                ]},
                {"component": "td", "props": {"class": "text-no-wrap"},
                 "text": self._state_cn(r["state"])},
                {"component": "td", "props": {"class": "text-no-wrap"}, "text": f"{r['progress']:.1f}%"},
                {"component": "td", "props": {"class": "text-no-wrap"}, "text": r["size"]},
                {"component": "td", "props": {"class": "text-no-wrap"}, "text": f"{r['ratio']:.2f}"},
                {"component": "td", "props": {"class": "text-no-wrap"}, "text": r["seeding"]},
                {"component": "td", "props": {"class": "text-caption"}, "text": r["tags"]},
                {"component": "td", "props": {"style": "min-width:230px;vertical-align:top"}, "content": [
                    {"component": "div", "props": {
                        "class": "d-flex flex-wrap justify-end",
                        "style": "min-width:230px",
                    }, "content": self._action_buttons(r)},
                    *[x for x in [self._tag_panel_for([r["hash"]], r["hash"], r)] if x],
                ]},
            ],
        }

    def _row_mobile(self, r: Dict[str, Any]) -> dict:
        meta = [
            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"},
             "text": r["title"]},
            {"component": "div", "props": {"class": "text-caption"},
             "text": f"状态：{self._state_cn(r['state'])}　进度：{r['progress']:.1f}%"},
            {"component": "div", "props": {"class": "text-caption"},
             "text": f"大小：{r['size']}　分享率：{r['ratio']:.2f}　做种：{r['seeding']}"},
            {"component": "div", "props": {"class": "text-caption"},
             "text": f"标签：{r['tags'] or '—'}　来源站：{r['site'] or '—'}"},
            {"component": "div", "props": {
                "class": "text-caption " + ("text-success" if r["is_mp"] else "text-medium-emphasis")},
             "text": f"MP管理：{'是' if r['is_mp'] else '否'}"},
        ]
        # 有海报才占左侧一栏；无海报时内容占满宽度（避免手机上大片留白）
        poster_col = (
            {"component": "VCol", "props": {"cols": 3, "class": "pa-1"},
             "content": [self._poster_img(r, 36)]}
            if r["poster"] else None
        )
        meta_col = {"component": "VCol", "props": {"cols": 9 if poster_col else 12, "class": "pa-1"},
                    "content": meta}
        return {
            "component": "VCard",
            "props": {"class": "mb-2 pa-2"},
            "content": [
                {"component": "VRow", "props": {"class": "ma-0"}, "content":
                    [c for c in (poster_col, meta_col) if c]},
                {"component": "div", "props": {"class": "d-flex flex-wrap mt-1"},
                 "content": self._action_buttons(r)},
            ],
        }
