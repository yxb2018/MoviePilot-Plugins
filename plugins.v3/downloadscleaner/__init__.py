"""
下载目录空间清理（DownloadsCleaner，MoviePilot V3 插件）

触发条件：监控盘可用空间低于阈值（默认 300 GiB）。
清理范围：仅配置的下载目录子树（默认 E:/Downloads），天然排除 SSD / 保种专区等其它目录。
目标条件：任务已完成 + 做种时长 >= 阈值 + 不带保护标签 + 分享率/大小/完成时间达标
          + 已确认媒体入库（MP 整理记录成功）。
清理动作：每轮刷新"待删除列队"（WEBUI 可见），空间低于阈值时才按做种时长从长到短删，
          直到空间回到目标线或达到单轮上限（可选连同本地文件、MP 历史记录、空目录回收）。
标签机制（无人值守，每轮自动执行，本插件仅管理唯一标签「未入库待确认」）：
          ① 带「未入库待确认」标签且 MP 整理记录出现成功（已入库确认）→ 自动移除标签，回归删除列队；
          ② 清理范围内、已完成、带 MP「已整理」标签且 MP 有整理记录但结果为失败 → 自动打上
             「未入库待确认」标签并提示，保护不删；
             注意：带「已整理」标签但 MP 无任何整理记录的种子不在此列（按用户约定不理会）。
          仍受保护的种子形成清单快照，在 WEBUI 顶部与消息通知中持续提示，
          直到 qB 里不再有该标签为止；用户是否处理都不影响插件运行。

设计参考：
- MoviePilot V3 官方插件开发规范（app.sdk.* 导入）；
- H&R助手（hitandrun）的服务注册与详情页模式；
- QbUploadLimiter（插件市场）的下载器/通知渠道自动识别与入库判断。
"""

import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db.oper.transferhistory import TransferHistoryOper
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.sdk.services import DownloaderHelper, ServiceConfigHelper
from app.schemas.types import MessageType, NotificationChannel

BS = chr(92)  # 反斜杠，避免源码转义歧义
GIB = 1024 ** 3
# MP 整理成功时由 Transfer 链路自动打上的 qB 标签（MP 内置行为，非本插件配置）
_ORGANIZED_TAG = "已整理"

# 配置里通知渠道名 -> SDK 枚举
_CHANNEL_MAP = {
    "Wechat": NotificationChannel.Wechat,
    "Feishu": NotificationChannel.Feishu,
    "WechatClawBot": NotificationChannel.WechatClawBot,
    "Telegram": NotificationChannel.Telegram,
    "Slack": NotificationChannel.Slack,
    "Discord": NotificationChannel.Discord,
    "DingTalk": NotificationChannel.DingTalk,
    "SynologyChat": NotificationChannel.SynologyChat,
    "VoceChat": NotificationChannel.VoceChat,
    "Web": NotificationChannel.Web,
    "WebPush": NotificationChannel.WebPush,
    "QQ": NotificationChannel.QQ,
}


class DownloadsCleaner(_PluginBase):
    """按磁盘可用空间阈值，自动清理下载目录中已入库、做种超时的下载任务。"""

    plugin_name = "下载目录空间清理"
    plugin_desc = ("磁盘可用空间低于阈值时，自动清理下载目录中已入库、做种超时的下载任务；"
                   "支持目录白名单、排除目录、保护标签（含未入库待确认标签自动打标与解除）、"
                   "完成保护、分享率门槛、通知渠道、两阶段删除与详情统计。")
    plugin_icon = "clean.png"
    plugin_version = "2.3.3"
    plugin_author = "yxb2018"
    plugin_config_prefix = "downloadscleaner_"
    plugin_order = 29
    auth_level = 1

    LOG_TAG = "[下载目录空间清理] "

    # ---------------- 运行态配置（init_plugin 收到配置后覆盖） ----------------

    _enabled = False
    _dry_run = True
    _onlyonce = False
    # ⚠️ 配置项属性不能与方法同名（_notify 是发送方法），否则实例属性会覆盖方法导致调用报错
    _notify_enabled = True
    _notify_channels: List[str] = []
    _cron = "0 */6 * * *"
    _monitor_path = "E:/"
    _threshold_free_gb = 300.0
    _target_free_gb = 350.0
    _downloaders: List[str] = []          # 空 = 全部已配置下载器
    _clean_dirs: List[str] = ["E:/Downloads"]
    _exclude_dirs: List[str] = []          # 排除目录：命中即不清理，优先于清理目录
    _block_keywords: List[str] = []        # 保护关键词：save_path/name 命中即跳过（保护，非删除名单）
    _min_seeding_days = 15
    _protect_tags: List[str] = ["保种", "H&R"]
    _protect_recent_days = 7
    _min_ratio = 0.0
    _min_size_gb = 0.0
    _require_library = True
    # 未入库待确认标签（本插件唯一管理的标签）：MP 已整理（带「已整理」标签）但整理记录为失败的种子，
    # 由插件自动打上该标签获得保护（不删）；MP 整理记录出现成功（已入库确认）后自动移除。
    # 带「已整理」标签但 MP 无任何整理记录的种子不在此列（按用户约定不理会）。
    _unconfirmed_tag = "未入库待确认"
    # 改名迁移用：旧版曾用名，仅用于把已保存配置 / 既有 qB 标签平滑迁到新名，迁移期内仍受保护
    _LEGACY_UNCONFIRMED_TAG = "已入库待确认"
    _delete_files = True
    _delete_history = True
    _clean_empty_dirs = False
    _tag_then_delete = False
    _delete_tag = "待清理"
    _max_delete_per_run = 30

    # 立即运行一次的自有调度器（框架托管周期任务之外的一次性任务）
    _once_scheduler: Optional[BackgroundScheduler] = None

    # ---------------------------------------------------------------- 生命周期

    def init_plugin(self, config: dict = None):
        # 防止保存配置重入 init_plugin 时残留旧的一次性调度器
        self._stop_once_scheduler()

        if not config:
            return

        self._enabled = bool(config.get("enabled"))
        self._dry_run = bool(config.get("dry_run", True))
        self._onlyonce = bool(config.get("onlyonce"))
        self._notify_enabled = bool(config.get("notify", True))
        self._notify_channels = self._to_list(config.get("notify_channels"))
        self._cron = str(config.get("cron") or self._cron).strip()
        self._monitor_path = str(config.get("monitor_path") or self._monitor_path).strip() or self._monitor_path
        self._threshold_free_gb = self._to_float(config.get("threshold_free_gb"), 300.0)
        self._target_free_gb = self._to_float(config.get("target_free_gb"), 350.0)
        self._downloaders = self._to_list(config.get("downloaders"))
        self._clean_dirs = self._to_list(config.get("clean_dirs")) or ["E:/Downloads"]
        self._exclude_dirs = self._to_list(config.get("exclude_dirs"))
        self._block_keywords = self._to_list(config.get("block_keywords"))
        self._min_seeding_days = max(0, int(self._to_float(config.get("min_seeding_days"), 15)))
        self._protect_tags = self._to_list(config.get("protect_tags")) or ["保种", "H&R"]
        self._protect_recent_days = max(0, int(self._to_float(config.get("protect_recent_days"), 7)))
        self._min_ratio = max(0.0, self._to_float(config.get("min_ratio"), 0.0))
        self._min_size_gb = max(0.0, self._to_float(config.get("min_size_gb"), 0.0))
        self._require_library = bool(config.get("require_library", True))
        # 未入库待确认标签：命中即保护；MP 整理记录成功（入库确认）后自动移除
        raw_tag = str(config.get("unconfirmed_tag") or self._unconfirmed_tag).strip()
        # 改名迁移：旧名「已入库待确认」自动映射为新名「未入库待确认」，无需手动改配置
        if raw_tag == self._LEGACY_UNCONFIRMED_TAG:
            raw_tag = self._unconfirmed_tag
        self._unconfirmed_tag = raw_tag
        self._delete_files = bool(config.get("delete_files", True))
        self._delete_history = bool(config.get("delete_history", True))
        self._clean_empty_dirs = bool(config.get("clean_empty_dirs"))
        self._tag_then_delete = bool(config.get("tag_then_delete"))
        self._delete_tag = str(config.get("delete_tag") or self._delete_tag).strip() or self._delete_tag
        self._max_delete_per_run = max(1, int(self._to_float(config.get("max_delete_per_run"), 30)))

        # 捕获"立即运行一次"意图：后续规范化写回 onlyonce=False，
        # 若写回触发同进程重入 init_plugin，重入调用会把 self._onlyonce 置 False，
        # 提前捕获保证本轮调度意图不丢
        run_once = self._onlyonce

        # 规范化持久化：修正非法组合（目标线低于阈值等），避免表单回显异常值
        try:
            normalized = self._current_config()
            if normalized != config:
                self.update_config(normalized)
        except Exception:
            pass

        # 立即运行一次：3 秒后执行一轮（参考市场插件 onlyonce 模式）
        if run_once:
            self._onlyonce = False
            self._once_scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._once_scheduler.add_job(
                func=self._clean,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name=f"{self.plugin_name}立即执行",
            )
            try:
                self._once_scheduler.start()
            except Exception as err:
                logger.warning(f"{self.LOG_TAG}立即执行任务启动失败：{err}")

        logger.info(
            f"{self.LOG_TAG}初始化完成：启用={self._enabled} 演练={self._dry_run} "
            f"监控={self._monitor_path} 阈值={self._threshold_free_gb:.0f}GiB "
            f"目标={self._target_free_gb:.0f}GiB 目录={'/'.join(self._clean_dirs)} "
            f"做种>={self._min_seeding_days}天 周期={self._cron}"
        )

    def _current_config(self) -> Dict[str, Any]:
        """回读当前生效配置（用于规范化写回）。"""
        return {
            "enabled": self._enabled,
            "dry_run": self._dry_run,
            "onlyonce": False,
            "notify": self._notify_enabled,
            "notify_channels": self._notify_channels,
            "cron": self._cron,
            "monitor_path": self._monitor_path,
            "threshold_free_gb": self._threshold_free_gb,
            "target_free_gb": max(self._threshold_free_gb, self._target_free_gb),
            "downloaders": self._downloaders,
            "clean_dirs": self._clean_dirs,
            "exclude_dirs": self._exclude_dirs,
            "block_keywords": self._block_keywords,
            "min_seeding_days": self._min_seeding_days,
            "protect_tags": self._protect_tags,
            "protect_recent_days": self._protect_recent_days,
            "min_ratio": self._min_ratio,
            "min_size_gb": self._min_size_gb,
            "require_library": self._require_library,
            "unconfirmed_tag": self._unconfirmed_tag,
            "delete_files": self._delete_files,
            "delete_history": self._delete_history,
            "clean_empty_dirs": self._clean_empty_dirs,
            "tag_then_delete": self._tag_then_delete,
            "delete_tag": self._delete_tag,
            "max_delete_per_run": self._max_delete_per_run,
        }

    def get_state(self) -> bool:
        return self._enabled

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        """停止一次性调度器；周期任务由 get_service 框架托管。"""
        self._stop_once_scheduler()

    def _stop_once_scheduler(self):
        if self._once_scheduler:
            try:
                if self._once_scheduler.running:
                    self._once_scheduler.remove_all_jobs()
                    self._once_scheduler.shutdown(wait=False)
            except Exception as err:
                logger.debug(f"{self.LOG_TAG}停止一次性调度器异常：{err}")
            self._once_scheduler = None

    def get_service(self) -> List[Dict[str, Any]]:
        """注册周期检查服务，由 MP 插件框架托管调度。"""
        if not self._enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}cron 表达式无效 {self._cron}：{err}")
            return []
        return [
            {
                "id": "DownloadsCleaner",
                "name": self.plugin_name,
                "trigger": trigger,
                "func": self._clean,
                "kwargs": {},
            }
        ]

    # ---------------------------------------------------------------- 主流程

    def _clean(self):
        """调度入口：任何异常都记日志，避免整轮被静默终止。"""
        try:
            self._run_round()
        except Exception as err:
            logger.error(f"{self.LOG_TAG}本轮执行异常终止：{err}", exc_info=True)

    def _run_round(self):
        # 未入库待确认标签维护：一遍扫描（已整理但整理记录失败自动打标 / 入库成功自动摘标），
        # 与空间状态无关，每轮最先执行（只动标签不动任务，演练模式下同样生效）
        self._maintain_unconfirmed_tag()

        try:
            free_gb = self._free_gb(self._monitor_path)
        except Exception as err:
            logger.error(f"{self.LOG_TAG}读取监控盘空间失败 {self._monitor_path}：{err}")
            return
        if free_gb is None:
            logger.error(f"{self.LOG_TAG}监控盘路径不可用：{self._monitor_path}")
            return

        # 待删除列队每轮刷新（WEBUI 展示）；空间不足时才依规则删除
        candidates = self._collect_candidates()
        self.save_data("candidates", candidates[:50])
        self.save_data("disk", {
            "monitor": self._monitor_path,
            "free_gb": round(free_gb, 1),
            "threshold": self._threshold_free_gb,
            "target": self._target_free_gb,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        if free_gb >= self._threshold_free_gb:
            logger.info(
                f"{self.LOG_TAG}{self._monitor_path} 可用空间 {free_gb:.1f}GiB，"
                f"高于阈值 {self._threshold_free_gb:.0f}GiB，无需清理；"
                f"待删除列队 {len(candidates)} 个"
            )
            return

        mode = "演练" if self._dry_run else "执行"
        logger.info(
            f"{self.LOG_TAG}触发清理（{mode}）：{self._monitor_path} 可用 {free_gb:.1f}GiB "
            f"< 阈值 {self._threshold_free_gb:.0f}GiB，目标回到 {self._target_free_gb:.0f}GiB，"
            f"待删除列队 {len(candidates)} 个"
        )

        if not candidates:
            message = (
                f"{self._monitor_path} 可用空间 {free_gb:.1f}GiB 已低于阈值 "
                f"{self._threshold_free_gb:.0f}GiB，但没有符合清理条件的下载任务"
            )
            logger.warning(f"{self.LOG_TAG}{message}")
            # 标题由 _notify 统一加【插件名】前缀，正文不再重复
            self._notify(message)
            return

        deleted: List[Dict[str, Any]] = []
        tagged: List[Dict[str, Any]] = []
        failed = 0
        freed_bytes = 0
        for item in candidates[: self._max_delete_per_run]:
            if not self._dry_run:
                current = self._free_gb(self._monitor_path)
                if current is not None and current >= self._target_free_gb:
                    logger.info(f"{self.LOG_TAG}可用空间已回到目标线 {self._target_free_gb:.0f}GiB，停止本轮清理")
                    break
            if self._tag_then_delete and self._delete_tag not in item["tags"]:
                if self._add_tag(item):
                    tagged.append(item)
                    logger.info(
                        f"{self.LOG_TAG}已打标签 [{item['name'][:60]}]（{self._delete_tag}，下轮删除）"
                    )
                else:
                    failed += 1
                time.sleep(0.3)
                continue
            if self._delete_one(item):
                deleted.append(item)
                freed_bytes += item["size"]
                logger.info(
                    f"{self.LOG_TAG}已删除任务 [{item['name'][:60]}] "
                    f"{item['size'] / GIB:.2f}GiB 做种 {item['seed_days']:.1f} 天"
                )
            else:
                failed += 1
                logger.warning(f"{self.LOG_TAG}删除失败 [{item['name'][:60]}]")
            time.sleep(0.3)

        if self._clean_empty_dirs and not self._dry_run:
            removed_dirs = self._recycle_empty_dirs()
            if removed_dirs:
                logger.info(f"{self.LOG_TAG}空目录回收 {removed_dirs} 个")

        self._record_run(deleted, freed_bytes, failed, tagged, mode)

        summary = (
            f"{mode}完成\n"
            f"监控盘：{self._monitor_path}\n"
            f"删除任务：{len(deleted)} 个，释放 {freed_bytes / GIB:.2f}GiB"
        )
        if tagged:
            summary += f"\n标记待清理：{len(tagged)} 个（下轮删除）"
        if failed:
            summary += f"\n失败：{failed} 个"
        current = self._free_gb(self._monitor_path)
        if current is not None:
            summary += f"\n当前可用：{current:.1f}GiB（目标 {self._target_free_gb:.0f}GiB）"
        if deleted:
            summary += "\n明细：\n" + "\n".join(
                f"- {item['name'][:56]}（{item['size'] / GIB:.1f}GiB / 做种 {item['seed_days']:.0f} 天）"
                for item in deleted[:10]
            )
            if len(deleted) > 10:
                summary += f"\n- ……等共 {len(deleted)} 个"
        logger.info(summary.replace("\n", " | "))
        self._notify(summary)

    # ---------------------------------------------------------------- 候选收集

    def _collect_candidates(self) -> List[Dict[str, Any]]:
        roots = [self._norm_root(d) for d in self._clean_dirs if str(d).strip()]
        if not roots:
            logger.error(f"{self.LOG_TAG}未配置有效的清理目录，取消本轮")
            return []
        # 排除目录优先于清理目录：先算好，命中子树直接跳过
        exclude_roots = [self._norm_root(d) for d in self._exclude_dirs if str(d).strip()]

        transfer_oper = TransferHistoryOper()
        protected = {t.strip() for t in self._protect_tags if t.strip()}
        # 未入库待确认标签（含改名前的旧名）命中即跳过（疑似入库失败的种子一律不删）
        if self._unconfirmed_tag:
            protected.add(self._unconfirmed_tag)
        # 兼容改名前的旧标签名，迁移期内同样保护
        protected.add(self._LEGACY_UNCONFIRMED_TAG)
        block_words = [k.strip() for k in self._block_keywords if k.strip()]
        services = self._get_services()
        if not services:
            return []

        candidates: List[Dict[str, Any]] = []
        for downloader, service in services.items():
            instance = getattr(service, "instance", None)
            if not instance or not hasattr(instance, "get_torrents"):
                continue
            try:
                torrents, error_flag = instance.get_torrents(status="completed")
            except Exception as err:
                logger.warning(f"{self.LOG_TAG}读取 {downloader} 任务失败：{err}")
                continue
            if error_flag:
                logger.warning(f"{self.LOG_TAG}读取 {downloader} 任务返回错误标志，跳过该下载器")
                continue
            for torrent in torrents or []:
                item = self._match_torrent(
                    torrent, downloader, roots, exclude_roots, protected, block_words,
                    transfer_oper,
                )
                if item:
                    candidates.append(item)

        # 做种时长最长的优先清理
        candidates.sort(key=lambda c: -c["seed_days"])
        logger.info(
            f"{self.LOG_TAG}候选扫描完成：命中 {len(candidates)} 个"
            f"（目录={'/'.join(roots)}，排除目录={'/'.join(exclude_roots) or '无'}，"
            f"做种≥{self._min_seeding_days}天，完成保护≥{self._protect_recent_days}天，"
            f"排除标签={protected or '无'}，要求入库={'开' if self._require_library else '关'}）"
        )
        return candidates

    def _get_services(self) -> Dict[str, Any]:
        """获取已配置且可连接的下载器实例。"""
        try:
            services = DownloaderHelper().get_services(
                name_filters=self._downloaders or None
            ) or {}
        except Exception as err:
            logger.error(f"{self.LOG_TAG}获取下载器列表失败：{err}")
            return {}
        active = {}
        for name, service in services.items():
            instance = getattr(service, "instance", None)
            if not instance:
                logger.warning(f"{self.LOG_TAG}下载器 [{name}] 实例不存在，跳过")
                continue
            try:
                if instance.is_inactive():
                    logger.warning(f"{self.LOG_TAG}下载器 [{name}] 未连接，跳过")
                    continue
            except Exception:
                pass
            active[name] = service
        if not active:
            logger.warning(f"{self.LOG_TAG}没有可连接的下载器，取消本轮")
        return active

    # ---------------------------------------------------------------- 未入库待确认标签

    def _maintain_unconfirmed_tag(self):
        """未入库待确认标签维护（每轮最先执行，与空间状态无关，演练模式下同样生效）：
        一遍扫描双方向——已整理但整理记录为失败（存在失败记录）的种子自动打标保护，
        MP 整理记录出现成功（已入库确认）自动摘标回归删除列队；
        带「已整理」标签但 MP 无任何整理记录的种子不理会；
        仍受保护的种子形成清单快照，在 WEBUI 顶部与消息通知中持续提示，
        直到 qB 里不再有该标签为止；用户是否处理都不影响插件运行。"""
        if not self._unconfirmed_tag:
            # 标签名为空：清掉历史提示，避免残留
            if self.get_data("tag_protected"):
                self.save_data("tag_protected", None)
            return
        try:
            tagged, released, protected, scanned = self._scan_unconfirmed_tag()
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}保护标签扫描异常：{err}")
            return
        if not scanned:
            # 没有成功读取过任何下载器（qB 整体不可用）：保留上轮提示，不清空不通知
            logger.debug(f"{self.LOG_TAG}保护标签扫描未读到下载器，保留上轮提示")
            return

        # 受保护清单快照更新
        old_snapshot = self.get_data("tag_protected") or {}
        old_hashes = {i.get("hash") for i in (old_snapshot.get("items") or [])}
        new_hashes = {p.get("hash") for p in protected}
        self.save_data("tag_protected", {
            "tag": self._unconfirmed_tag,
            "items": protected,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        logger.info(
            f"{self.LOG_TAG}标签维护完成：新保护 {len(tagged)} 个，"
            f"自动解除 {len(released)} 个，仍受保护 {len(protected)} 个"
        )
        # 没有任何可说的内容就不发：正文只剩标题行的空通知只会制造噪音
        # （保护清单变化但没有新打标/新摘标时，只记日志）
        if not (tagged or released or protected):
            if old_hashes != new_hashes:
                logger.info(f"{self.LOG_TAG}受保护清单已清空（{len(old_hashes)} → 0），不发送通知")
            return
        lines = ["未入库待确认标签维护"]
        if tagged:
            lines.append(f"- {len(tagged)} 个种子已整理但 MP 整理记录为失败，"
                         f"已打保护标签「{self._unconfirmed_tag}」：")
            lines.extend(f"  · {name}" for name in tagged)
        if released:
            lines.append(f"- {len(released)} 个种子已确认入库，自动移除标签"
                         f"「{self._unconfirmed_tag}」：")
            lines.extend(f"  · {name}" for name in released)
        if protected:
            names = "、".join(p["name"][:30] for p in protected[:10])
            lines.append(f"- 仍受保护 {len(protected)} 个（跳过清理）：{names}"
                         + (f" 等共 {len(protected)} 个" if len(protected) > 10 else ""))
        self._notify("\n".join(lines))

    def _tag_applied(self, instance: Any, hash_: str) -> bool:
        """回读种子标签，确认保护标签确实已打上（qB 打标签封装无返回值）。"""
        try:
            torrents, error_flag = instance.get_torrents(ids=[hash_])
        except Exception:
            return False
        if error_flag or not torrents:
            return False
        tags = {t.strip() for t in str(torrents[0].get("tags") or "").split(",") if t.strip()}
        return self._unconfirmed_tag in tags

    def _scan_unconfirmed_tag(self) -> Tuple[List[str], List[str], List[Dict[str, Any]], bool]:
        """未入库待确认标签一遍扫描（双方向，无人值守）：
        ① 已带「未入库待确认」标签：MP 整理记录出现成功（入库确认）→ 自动移除标签，回归删除列队；
        ② 未带标签：清理范围内、已完成、带 MP「已整理」标签且 MP 有整理记录但结果为失败
           → 自动打上「未入库待确认」标签保护，并计入提示清单。
           带「已整理」标签但 MP 无任何整理记录的种子不在此列（按用户约定不理会）。
        返回 (新打标签名, 已自动摘标签名, 仍受保护清单, 是否成功读取过下载器)。"""
        tagged: List[str] = []
        released: List[str] = []
        protected: List[Dict[str, Any]] = []
        scanned = False
        if not self._unconfirmed_tag:
            return tagged, released, protected, scanned
        services = self._get_services()
        if not services:
            return tagged, released, protected, scanned
        transfer_oper = TransferHistoryOper()
        # ②方向的作用范围：清理目录子树内（排除目录优先），不碰范围外的种子
        roots = [self._norm_root(d) for d in self._clean_dirs if str(d).strip()]
        exclude_roots = [self._norm_root(d) for d in self._exclude_dirs if str(d).strip()]
        protect_tags = {t.strip() for t in self._protect_tags if t.strip()}
        for downloader, service in services.items():
            instance = getattr(service, "instance", None)
            if not instance or not hasattr(instance, "get_torrents"):
                continue
            try:
                torrents, error_flag = instance.get_torrents()
            except Exception as err:
                logger.warning(f"{self.LOG_TAG}读取 {downloader} 任务失败：{err}")
                continue
            if error_flag:
                logger.warning(f"{self.LOG_TAG}读取 {downloader} 任务返回错误标志，跳过该下载器")
                continue
            scanned = True
            for torrent in torrents or []:
                torrent_hash = str(torrent.get("hash") or "").strip()
                if not torrent_hash:
                    continue
                name = str(torrent.get("name") or torrent_hash)
                tags = {t.strip() for t in str(torrent.get("tags") or "").split(",") if t.strip()}
                entry = {
                    "downloader": downloader,
                    "hash": torrent_hash,
                    "name": name,
                    "save_path": str(torrent.get("save_path") or ""),
                    "seed_days": round(int(torrent.get("seeding_time") or 0) / 86400, 1),
                    "size": int(torrent.get("size") or 0),
                }
                has_tag = self._unconfirmed_tag in tags or self._LEGACY_UNCONFIRMED_TAG in tags
                if has_tag:
                    # ① 已带标签：入库成功（有成功整理记录）→摘除标签回归删除列队；否则继续保护
                    if self._transfer_state(transfer_oper, torrent_hash, torrent) == "success":
                        for _t in (self._unconfirmed_tag, self._LEGACY_UNCONFIRMED_TAG):
                            if _t in tags:
                                try:
                                    instance.remove_torrents_tag(ids=[torrent_hash], tag=_t)
                                except Exception as err:
                                    logger.warning(f"{self.LOG_TAG}移除保护标签失败 [{name[:60]}]：{err}")
                        released.append(name[:60])
                        logger.info(
                            f"{self.LOG_TAG}[{name[:60]}] 已确认入库，自动移除保护标签"
                            f"「{self._unconfirmed_tag}」"
                        )
                        time.sleep(0.3)
                        continue
                    # 未入库（失败或记录缺失）：继续保护
                    protected.append(entry)
                    continue
                # ② 未带标签：只处理清理范围内、已完成、带 MP「已整理」标签的种子；
                #    已带其它保护标签的不重复打标；整理记录在的（入库确认）不动
                if not roots:
                    continue
                save_path = str(torrent.get("save_path") or "")
                if not self._in_roots(save_path, roots):
                    continue
                if exclude_roots and self._in_roots(save_path, exclude_roots):
                    continue
                if float(torrent.get("progress") or 0) < 1:
                    continue
                if protect_tags & tags:
                    continue
                if _ORGANIZED_TAG not in tags:
                    continue
                # 仅「已整理」且 MP 有整理记录但结果为失败（failed）才打标；
                # 已入库成功（success）无需保护；已整理但无任何记录（none）按约定不理会
                state = self._transfer_state(transfer_oper, torrent_hash, torrent)
                if state != "failed":
                    continue
                try:
                    instance.set_torrents_tag(ids=[torrent_hash], tags=[self._unconfirmed_tag])
                except Exception as err:
                    logger.warning(f"{self.LOG_TAG}打保护标签失败 [{name[:60]}]：{err}")
                    continue
                # 回读校验：qB 打标签封装无返回值，避免假成功漏保护
                if not self._tag_applied(instance, torrent_hash):
                    logger.warning(f"{self.LOG_TAG}保护标签回读校验未通过 [{name[:60]}]，下轮重试")
                    continue
                tagged.append(name[:60])
                protected.append(entry)
                logger.info(
                    f"{self.LOG_TAG}[{name[:60]}] 已整理但 MP 整理记录为失败，"
                    f"已打保护标签「{self._unconfirmed_tag}」"
                )
                time.sleep(0.3)
        return tagged, released, protected, scanned

    def _match_torrent(
        self,
        torrent: Any,
        downloader: str,
        roots: List[str],
        exclude_roots: List[str],
        protected: Set[str],
        block_words: List[str],
        transfer_oper: TransferHistoryOper,
    ) -> Optional[Dict[str, Any]]:
        try:
            torrent_hash = str(torrent.get("hash") or "").strip()
            if not torrent_hash:
                return None
            save_path = str(torrent.get("save_path") or "")
            name = str(torrent.get("name") or torrent_hash)
            # 排除目录优先：命中排除子树直接跳过（即使位于清理目录内）
            if exclude_roots and self._in_roots(save_path, exclude_roots):
                return None
            if not self._in_roots(save_path, roots):
                return None
            # 路径黑名单
            if block_words:
                haystack = (save_path + " " + name).lower()
                if any(word.lower() in haystack for word in block_words):
                    return None
            tags = {t.strip() for t in str(torrent.get("tags") or "").split(",") if t.strip()}
            if tags & protected:
                return None
            if float(torrent.get("progress") or 0) < 1:
                return None
            seeding = int(torrent.get("seeding_time") or 0)
            if seeding < self._min_seeding_days * 86400:
                return None
            if self._protect_recent_days > 0:
                completion = int(torrent.get("completion_on") or 0)
                if completion > 0 and (time.time() - completion) < self._protect_recent_days * 86400:
                    return None
            if self._min_ratio > 0 and float(torrent.get("ratio") or 0) < self._min_ratio:
                return None
            size = int(torrent.get("size") or 0)
            if self._min_size_gb > 0 and size < self._min_size_gb * GIB:
                return None
            in_library = False
            if self._require_library:
                in_library = self._transfer_state(transfer_oper, torrent_hash, torrent) == "success"
                if not in_library:
                    return None
            return {
                "downloader": downloader,
                "hash": torrent_hash,
                "name": name,
                "save_path": save_path,
                "seed_days": seeding / 86400,
                "ratio": float(torrent.get("ratio") or 0),
                "size": size,
                "tags": sorted(tags),
                "in_library": in_library,
            }
        except Exception:
            return None

    # ---------------------------------------------------------------- 入库核验

    def _transfer_state(
        self,
        transfer_oper: TransferHistoryOper,
        torrent_hash: str,
        torrent: Optional[Dict[str, Any]] = None,
    ) -> str:
        """查询该 hash 的 MP 整理记录状态（核心判定，3 态）：
        - "success": 存在成功且目标在媒体库内的记录（已入库，可删）
        - "failed":  存在整理记录但均非成功入库（MP 已整理但结果是失败）
        - "none":    无任何整理记录
        约定：「已整理」标签 + 有失败记录 = 未入库待确认（需人工核查，插件自动保护）；
        「已整理」标签 + 无任何记录 = 无需理会（MP 已加标签但无记录，非本插件职责）。

        路径兜底：手动重识别（重新整理）的转移记录 download_hash 可能被写成 None，
        导致 list_by_hash 落空而误判为未入库。此时改用 qB 种子 save_path+name 推导的
        内容根目录，按 TransferHistoryOper.list_success_by_src(src, recursive=True)
        做路径兜底查询（合规：仅经 Oper 访问宿主数据，不 import 宿主 Model）。"""
        try:
            records = transfer_oper.list_by_hash(torrent_hash) or []
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}查询整理记录失败 {torrent_hash[:12]}：{err}")
            records = []
        state = self._judge_records(records)
        if state is not None:
            return state
        # 兜底：按源路径递归查成功记录（兼容 download_hash 为 None 的手动重传记录）
        if torrent:
            try:
                src_root = self._content_root(torrent)
                if src_root:
                    fallback = transfer_oper.list_success_by_src(src_root, recursive=True) or []
                    state = self._judge_records(fallback)
                    if state is not None:
                        return state
            except Exception as err:
                logger.warning(
                    f"{self.LOG_TAG}源路径兜底查询失败 {str(torrent.get('name', ''))[:40]}：{err}"
                )
        return "none"

    @staticmethod
    def _judge_records(records: Any) -> Optional[str]:
        """统一判定整理记录列表：存在 status 且 dest 以 /Media/ 开头的记录 -> success；
        有记录但无成功 -> failed；空 -> None（交由调用方决定）。"""
        if not records:
            return None
        for record in records:
            dest = str(getattr(record, "dest", "") or "")
            if getattr(record, "status", None) and dest.startswith("/Media/"):
                return "success"
        return "failed"

    def _content_root(self, torrent: Dict[str, Any]) -> str:
        """由 qB 种子 save_path + name 推导内容根目录（归一化，兼容 Windows 反斜杠）。"""
        save_path = str(torrent.get("save_path") or "").strip()
        name = str(torrent.get("name") or "").strip()
        if not save_path or not name:
            return ""
        root = os.path.normpath(os.path.join(save_path, name))
        return root.replace("\\", "/")

    # ---------------------------------------------------------------- 删除动作

    def _delete_one(self, item: Dict[str, Any]) -> bool:
        try:
            service = DownloaderHelper().get_service(name=item["downloader"])
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}获取下载器实例失败 {item['downloader']}：{err}")
            return False
        instance = getattr(service, "instance", None) if service else None
        if not instance or not hasattr(instance, "delete_torrents"):
            return False
        if self._dry_run:
            logger.info(
                f"{self.LOG_TAG}[演练] 将删除 [{item['name'][:60]}]"
                f"（delete_file={self._delete_files}）"
            )
            return True
        try:
            ok = bool(
                instance.delete_torrents(delete_file=self._delete_files, ids=[item["hash"]])
            )
        except Exception as err:
            logger.error(f"{self.LOG_TAG}删除任务失败 [{item['name'][:60]}]：{err}")
            return False
        if ok and self._delete_history:
            self._clean_history(item["hash"], item["name"])
        return ok

    def _add_tag(self, item: Dict[str, Any]) -> bool:
        """两阶段删除：先打标签，下一轮才删。"""
        try:
            service = DownloaderHelper().get_service(name=item["downloader"])
            instance = getattr(service, "instance", None) if service else None
            if not instance or not hasattr(instance, "set_torrents_tag"):
                return False
            return bool(instance.set_torrents_tag(ids=item["hash"], tags=[self._delete_tag]))
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}打标签失败 [{item['name'][:60]}]：{err}")
            return False

    def _clean_history(self, torrent_hash: str, name: str):
        """同步清理 MP 整理/下载历史记录，避免删除任务后残留幽灵记录。"""
        try:
            from app.db.oper.downloadhistory import DownloadHistoryOper
            transfer_oper = TransferHistoryOper()
            for record in transfer_oper.list_by_hash(torrent_hash) or []:
                try:
                    transfer_oper.delete(record.id)
                except Exception as err:
                    logger.warning(f"{self.LOG_TAG}清理整理记录失败 [{name[:40]}]：{err}")
            dl_oper = DownloadHistoryOper()
            history = dl_oper.get_by_hash(torrent_hash)
            if history:
                dl_oper.delete_history(history.id)
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}清理下载历史失败 [{name[:40]}]：{err}")

    def _recycle_empty_dirs(self) -> int:
        """回收清理目录子树下的空目录（自底向上，限量防护）。"""
        removed = 0
        roots = [self._norm_root(d) for d in self._clean_dirs if str(d).strip()]
        for root in roots:
            try:
                walker = list(os.walk(root, topdown=False))
            except Exception:
                continue
            root_norm = os.path.normpath(root).upper()
            for dirpath, _dirnames, _filenames in walker:
                if os.path.normpath(dirpath).upper() == root_norm:
                    continue
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                        removed += 1
                        if removed >= 200:
                            return removed
                except Exception:
                    continue
        return removed

    # ---------------------------------------------------------------- 统计与通知

    def _record_run(self, deleted, freed_bytes, failed, tagged, mode):
        statistic = self.get_data("statistic") or {}
        # 演练轮的删除为模拟值，只更新本轮展示，不计入累计
        is_real = mode != "演练"
        statistic.update({
            "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_deleted": len(deleted),
            "last_freed_gb": round(freed_bytes / GIB, 2),
            "last_failed": failed,
            "last_tagged": len(tagged),
            "last_mode": mode,
            "total_deleted": int(statistic.get("total_deleted") or 0) + (len(deleted) if is_real else 0),
            "total_freed_gb": round(float(statistic.get("total_freed_gb") or 0)
                                    + (freed_bytes / GIB if is_real else 0), 2),
        })
        self.save_data("statistic", statistic)

        if deleted:
            history = self.get_data("history") or []
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for item in deleted:
                history.append({
                    "name": item["name"],
                    "size_gb": round(item["size"] / GIB, 2),
                    "seed_days": round(item["seed_days"], 1),
                    "save_path": item["save_path"],
                    "downloader": item["downloader"],
                    "mode": mode,
                    "time": now,
                })
            self.save_data("history", history[-100:])

    def _notify(self, text: str):
        """发送插件通知。

        标题统一由这里加【插件名】前缀，调用方正文不要再带同名前缀，
        否则通知中心会把标题与首行正文渲染成重复的两行。
        渠道为多选，需要逐个发送；未选渠道时传 None 交给宿主默认路由。
        """
        if not self._notify_enabled:
            return
        channels = [c for c in (_CHANNEL_MAP.get(ch) for ch in self._notify_channels) if c]
        # channels 为空 -> [None]，表示按宿主默认渠道发送
        targets = list(channels) if channels else [None]
        title = f"【{self.plugin_name}】"

        def _send():
            for target in targets:
                try:
                    self.post_message(
                        channel=target,
                        mtype=MessageType.Plugin,
                        title=title,
                        text=text,
                    )
                except Exception as err:
                    logger.debug(f"{self.LOG_TAG}通知发送失败（{target}）：{err}")

        # 即发即走：通知通道可能阻塞（网络/渠道不可用），绝不能卡住清理主流程
        threading.Thread(target=_send, name="DownloadsCleanerNotify", daemon=True).start()

    # ---------------------------------------------------------------- 工具

    def _free_gb(self, path: str) -> Optional[float]:
        try:
            return shutil.disk_usage(path).free / GIB
        except Exception:
            return None

    @staticmethod
    def _norm_root(root: Any) -> str:
        return str(root).strip().replace("/", BS).rstrip(BS).upper()

    @staticmethod
    def _in_roots(path: str, roots: List[str]) -> bool:
        norm = str(path or "").replace("/", BS).rstrip(BS).upper()
        return any(norm == root or norm.startswith(root + BS) for root in roots)

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return [str(v).strip() for v in value if str(v).strip()]
        text = str(value)
        for sep in ("\r\n", "\n", ",", "，"):
            text = text.replace(sep, "\n")
        return [line.strip() for line in text.split("\n") if line.strip()]

    # ---------------------------------------------------------------- 配置表单

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 下载器下拉：MoviePilot 已配置并启用的下载器
        downloader_items = []
        try:
            for conf in ServiceConfigHelper.get_downloader_configs() or []:
                conf_name = getattr(conf, "name", "") or ""
                conf_type = getattr(conf, "type", "") or ""
                if conf_name and getattr(conf, "enabled", False):
                    downloader_items.append({"title": f"{conf_name}（{conf_type}）", "value": conf_name})
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取下载器配置失败：{err}")

        # 通知渠道下拉：MoviePilot 已启用渠道
        notify_items = []
        try:
            for conf in ServiceConfigHelper.get_notification_configs() or []:
                conf_type = getattr(conf, "type", "") or ""
                conf_name = getattr(conf, "name", "") or conf_type
                if conf_type and {"title": conf_name, "value": conf_type} not in notify_items:
                    notify_items.append({"title": conf_name, "value": conf_type})
        except Exception as err:
            logger.warning(f"{self.LOG_TAG}读取通知配置失败：{err}")

        # 表单最顶部：受保护种子提示（动态读取，无受保护种子时不渲染）
        banner_row = self._tag_banner_row(limit=5)
        return [
            {
                "component": "VForm",
                "content": ([banner_row] if banner_row else []) + [
                    # 第一行：总开关
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 3, self._switch("enabled", "启用插件",
                                "开启后按周期自动检查磁盘空间并清理")),
                            self._col(12, 3, self._switch("dry_run", "演练模式（只记录不删除）",
                                "建议先演练观察日志，确认候选符合预期后再关闭")),
                            self._col(12, 3, self._switch("onlyonce", "立即运行一次",
                                "保存配置后 3 秒执行一轮，执行后自动关闭")),
                            self._col(12, 3, self._switch("notify", "开启通知",
                                "每轮清理结果推送给配置的渠道")),
                        ],
                    },
                    # 第二行：触发条件
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 4, {
                                "component": "VCronField",
                                "props": {"model": "cron", "label": "检查周期",
                                          "placeholder": "0 */6 * * *",
                                          "hint": "cron 表达式，默认每 6 小时", "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VTextField",
                                "props": {"model": "monitor_path", "label": "监控盘目录",
                                          "placeholder": "E:/",
                                          "hint": "读取该路径所在磁盘的可用空间", "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VTextField",
                                "props": {"model": "threshold_free_gb", "label": "触发阈值（可用空间 GiB）",
                                          "type": "number", "min": "0", "placeholder": "300",
                                          "hint": "低于该值开始清理", "persistent-hint": True},
                            }),
                        ],
                    },
                    # 第三行：目标与范围
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 4, {
                                "component": "VTextField",
                                "props": {"model": "target_free_gb", "label": "停止线（清理到该可用空间 GiB）",
                                          "type": "number", "min": "0", "placeholder": "350",
                                          "hint": "删到回线即停，为软目标", "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VSelect",
                                "props": {"model": "downloaders", "label": "下载器（多选，空=全部）",
                                          "items": downloader_items, "multiple": True, "chips": True,
                                          "clearable": True,
                                          "hint": "自动读取 MoviePilot 已配置的下载器，无需填地址密码",
                                          "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VSelect",
                                "props": {"model": "notify_channels", "label": "通知渠道（多选，空=系统默认）",
                                          "items": notify_items, "multiple": True, "chips": True,
                                          "clearable": True,
                                          "hint": "自动读取 MoviePilot 已启用的通知渠道",
                                          "persistent-hint": True},
                            }),
                        ],
                    },
                    # 第四行：过滤条件
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 4, {
                                "component": "VTextField",
                                "props": {"model": "min_seeding_days", "label": "最小做种天数",
                                          "type": "number", "min": "0", "placeholder": "15",
                                          "hint": "做种不满该天数不删", "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VTextField",
                                "props": {"model": "protect_recent_days", "label": "完成保护天数",
                                          "type": "number", "min": "0", "placeholder": "7",
                                          "hint": "下载完成不满该天数不删，保护不稳定窗口", "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VTextField",
                                "props": {"model": "max_delete_per_run", "label": "单轮最多删除任务数",
                                          "type": "number", "min": "1", "placeholder": "30",
                                          "hint": "单轮上限，防一次删太多", "persistent-hint": True},
                            }),
                        ],
                    },
                    # 第五行：更多过滤
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 4, {
                                "component": "VTextField",
                                "props": {"model": "min_ratio", "label": "分享率门槛（0=关闭）",
                                          "type": "number", "min": "0", "placeholder": "0",
                                          "hint": "分享率低于该值不删，防 H&R 追责", "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VTextField",
                                "props": {"model": "min_size_gb", "label": "小文件保护（GB，0=关闭）",
                                          "type": "number", "min": "0", "placeholder": "0",
                                          "hint": "小于该大小的任务不删", "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VTextField",
                                "props": {"model": "delete_tag", "label": "两阶段删除标签名",
                                          "placeholder": "待清理",
                                          "hint": "两阶段模式下先打该标签，下轮才删", "persistent-hint": True},
                            }),
                        ],
                    },
                    # 第六行：目录与标签
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 4, {
                                "component": "VTextarea",
                                "props": {"model": "clean_dirs",
                                          "label": "清理目录（每行一个，仅这些目录子树内的任务才会被清理）",
                                          "rows": 3, "placeholder": "E:/Downloads",
                                          "hint": "SSD / 保种目录不要加进来", "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VTextarea",
                                "props": {"model": "exclude_dirs",
                                          "label": "排除目录（每行一个，命中即不清理，优先于清理目录）",
                                          "rows": 3, "placeholder": "E:/Downloads/保种中",
                                          "hint": "位于清理目录内但需要跳过的子目录，填完整路径前缀",
                                          "persistent-hint": True},
                            }),
                            self._col(12, 4, {
                                "component": "VTextarea",
                                "props": {"model": "protect_tags",
                                          "label": "保护标签（命中任一标签的任务不删除，每行一个）",
                                          "rows": 3, "placeholder": "保种\nH&R",
                                          "persistent-hint": True},
                            }),
                        ],
                    },
                    # 第七行：未入库待确认标签
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 6, {
                                "component": "VTextField",
                                "props": {"model": "unconfirmed_tag", "label": "未入库待确认标签名",
                                          "placeholder": "未入库待确认",
                                          "hint": "本插件唯一管理的标签。清理范围内「已整理且 MP 整理记录为失败」的"
                                                  "种子自动打上该标签保护；已入库成功（整理记录成功）后自动移除",
                                          "persistent-hint": True},
                            }),
                            self._col(12, 6, {
                                "component": "VAlert",
                                "props": {"type": "info", "variant": "tonal",
                                          "text": "标签机制：本插件仅管理唯一标签「未入库待确认」。已整理但 MP 整理记录"
                                                  "为失败的种子由插件自动打上该标签保护并提示；带「已整理」但 MP 无"
                                                  "任何记录的种子不理会；整理记录成功确认入库后插件自动摘除，回归删除列队"},
                            }),
                        ],
                    },
                    # 第八行：保护关键词
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 12, {
                                "component": "VTextarea",
                                "props": {"model": "block_keywords",
                                          "label": "保护关键词（每行一个，路径或名称命中即跳过清理）",
                                          "rows": 2, "placeholder": "migration",
                                          "hint": "注意：这是保护名单（命中不删），不是删除名单",
                                          "persistent-hint": True},
                            }),
                        ],
                    },
                    # 第九行：动作开关
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 4, self._switch("delete_files", "同时删除本地文件",
                                "关闭则只删任务、保留文件（不回收空间）")),
                            self._col(12, 4, self._switch("delete_history", "同步清理 MP 历史记录",
                                "删除任务同时清理整理记录与下载记录")),
                            self._col(12, 4, self._switch("require_library", "要求媒体已入库",
                                "整理记录成功或额外白名单命中才删（强烈建议保持开启）")),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(12, 4, self._switch("clean_empty_dirs", "回收空目录",
                                "删除后清理清理目录子树下的空目录")),
                            self._col(12, 4, self._switch("tag_then_delete", "两阶段删除",
                                "第一轮只打标签，下一轮才真删，留出人工干预窗口")),
                            self._col(12, 4, {
                                "component": "VAlert",
                                "props": {"type": "error", "variant": "tonal",
                                          "text": "警告：qBittorrent 删除任务并删文件为永久删除、不进回收站，请谨慎开启"},
                            }),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "dry_run": True,
            "onlyonce": False,
            "notify": True,
            "notify_channels": [],
            "cron": "0 */6 * * *",
            "monitor_path": "E:/",
            "threshold_free_gb": 300,
            "target_free_gb": 350,
            "downloaders": [],
            "clean_dirs": "E:/Downloads",
            "exclude_dirs": "",
            "block_keywords": "",
            "min_seeding_days": 15,
            "protect_tags": "保种\nH&R",
            "protect_recent_days": 7,
            "min_ratio": 0,
            "min_size_gb": 0,
            "require_library": True,
            "unconfirmed_tag": "未入库待确认",
            "delete_files": True,
            "delete_history": True,
            "clean_empty_dirs": False,
            "tag_then_delete": False,
            "delete_tag": "待清理",
            "max_delete_per_run": 30,
        }

    # ---------------------------------------------------------------- 详情页

    def _tag_banner_text(self, limit: int = 0) -> Optional[str]:
        """受保护种子提示文本（WEBUI 顶部展示）：无受保护种子时返回 None。
        数据来自每轮标签维护的快照，qB 里标签消失后下一轮自动不再提示。"""
        snapshot = self.get_data("tag_protected") or {}
        items = snapshot.get("items") or []
        if not items or snapshot.get("tag") != self._unconfirmed_tag:
            return None
        names = [str(i.get("name") or "")[:40] for i in items]
        shown = names[:limit] if limit else names
        text = (f"当前 {len(items)} 个种子带保护标签「{self._unconfirmed_tag}」，已跳过清理："
                + "、".join(shown))
        if limit and len(names) > limit:
            text += f" 等共 {len(items)} 个"
        text += (f"（数据时间 {snapshot.get('time', '-')}；确认入库后插件自动移除标签，"
                 f"也可在 qB 手动移除；是否处理都不影响插件运行）")
        return text

    def _tag_banner_row(self, limit: int = 5) -> Optional[dict]:
        """设置页表单顶部的受保护种子提示行；无提示内容时返回 None。"""
        text = self._tag_banner_text(limit=limit)
        if not text:
            return None
        return {
            "component": "VRow",
            "content": [
                self._col(12, 12, {
                    "component": "VAlert",
                    "props": {"type": "warning", "variant": "tonal", "text": text},
                }),
            ],
        }

    def get_page(self) -> Optional[List[dict]]:
        statistic = self.get_data("statistic") or {}
        disk = dict(self.get_data("disk") or {})
        # 磁盘卡优先展示实时读数，失败时回退最近一次运行的快照
        live_gb = self._free_gb(self._monitor_path)
        if live_gb is not None:
            disk["free_gb"] = round(live_gb, 1)
            disk["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "（实时）"
        candidates = self.get_data("candidates") or []
        history = self.get_data("history") or []

        # 顶部统计卡片
        cards = []
        for title, value, hint in (
            ("累计删除", f"{statistic.get('total_deleted') or 0} 个", "总量"),
            ("累计释放", f"{float(statistic.get('total_freed_gb') or 0):.1f} GiB", "总量"),
            ("本轮删除", f"{statistic.get('last_deleted') or 0} 个", statistic.get("last_mode") or "-"),
            ("当前可用", f"{disk.get('free_gb', 0):.1f} GiB", disk.get("checked_at") or "未检测"),
        ):
            cards.append(
                self._col(6, 3, {
                    "component": "VCard",
                    "props": {"variant": "tonal"},
                    "content": [
                        {"component": "VCardText", "props": {"class": "pa-2 text-h6"}, "text": str(value)},
                        {"component": "VCardText", "props": {"class": "pa-2 pt-0 text-caption"},
                         "text": f"{title} · {hint}"},
                    ],
                })
            )

        # 磁盘状态卡
        disk_card = self._col(12, 12, {
            "component": "VAlert",
            "props": {
                "type": "info", "variant": "tonal",
                "text": (f"监控盘 {disk.get('monitor', self._monitor_path)}："
                         f"可用 {disk.get('free_gb', 0):.1f} GiB，"
                         f"阈值 {disk.get('threshold', self._threshold_free_gb):.0f} GiB，"
                         f"目标线 {disk.get('target', self._target_free_gb):.0f} GiB，"
                         f"检测时间 {disk.get('checked_at', '-')}"),
            },
        })

        # 未入库待确认标签说明卡（配置态提示；受保护清单见顶部横幅）
        tag_card = self._col(12, 12, {
            "component": "VAlert",
            "props": {
                "type": "info", "variant": "tonal",
                "text": (f"未入库待确认标签「{self._unconfirmed_tag or '未配置'}」（本插件唯一标签）：清理范围内"
                         f"「已整理且 MP 整理记录为失败」的种子自动打上该标签保护，qB 中命中该标签的种子"
                         f"一律不删；MP 整理记录成功（已入库确认）后自动移除标签，回归删除列队"),
            },
        })

        blocks: List[dict] = []
        # 顶部：受保护种子提示（无受保护种子时不渲染）
        banner_text = self._tag_banner_text(limit=15)
        if banner_text:
            blocks.append({"component": "VRow", "content": [self._col(12, 12, {
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal", "text": banner_text},
            })]})
        blocks.extend([
            {"component": "VRow", "content": cards},
            {"component": "VRow", "content": [disk_card]},
            {"component": "VRow", "content": [tag_card]},
        ])

        # 待删除列队表
        if candidates:
            blocks.append(self._table_card(
                "待删除列队（每轮刷新；空间低于阈值时按做种时长降序清理）",
                ["名称", "下载器", "大小(GiB)", "做种(天)", "分享率", "目录"],
                [[
                    {"component": "td", "text": c.get("name", "")[:60]},
                    {"component": "td", "text": c.get("downloader", "")},
                    {"component": "td", "text": f"{c.get('size', 0) / GIB:.1f}"},
                    {"component": "td", "text": f"{c.get('seed_days', 0):.1f}"},
                    {"component": "td", "text": f"{c.get('ratio', 0):.2f}"},
                    {"component": "td", "text": c.get("save_path", "")[:60]},
                ] for c in candidates[:30]]
            ))
        else:
            blocks.append(self._alert_card(
                "待删除列队",
                f"暂无符合条件的种子（清理目录：{'/'.join(self._clean_dirs)}，"
                f"做种≥{self._min_seeding_days}天，要求入库={'开' if self._require_library else '关'}）",
                "info"))

        # 删除历史表
        if history:
            rows = []
            for h in reversed(history[-30:]):
                rows.append([
                    {"component": "td", "text": h.get("name", "")[:60]},
                    {"component": "td", "text": h.get("time", "")},
                    {"component": "td", "text": f"{h.get('size_gb', 0):.1f}"},
                    {"component": "td", "text": f"{h.get('seed_days', 0):.0f}"},
                    {"component": "td", "text": h.get("mode", "")},
                ])
            blocks.append(self._table_card("最近删除记录", ["名称", "时间", "大小(GiB)", "做种(天)", "模式"], rows))
        else:
            blocks.append(self._alert_card("最近删除记录", "暂无删除记录", "info"))

        return blocks

    # ---------------------------------------------------------------- 表单/页面组件助手

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

    @staticmethod
    def _table_card(title: str, headers: List[str], rows: List[List[dict]]) -> dict:
        thead = [
            {"component": "th", "props": {"class": "text-start ps-4"}, "text": h}
            for h in headers
        ]
        return {
            "component": "VCard",
            "props": {"class": "mt-2"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": title},
                {
                    "component": "VTable",
                    "props": {"hover": True, "density": "comfortable"},
                    "content": [
                        {"component": "thead", "props": {"class": "text-no-wrap"}, "content": thead},
                        {"component": "tbody", "content": [
                            {"component": "tr", "props": {"class": "text-sm"}, "content": row}
                            for row in rows
                        ]},
                    ],
                },
            ],
        }

    @staticmethod
    def _alert_card(title: str, text: str, alert_type: str) -> dict:
        return {
            "component": "VCard",
            "props": {"class": "mt-2"},
            "content": [
                {"component": "VCardTitle", "props": {"class": "text-subtitle-1"}, "text": title},
                {
                    "component": "VCardText",
                    "content": [
                        {"component": "VAlert", "props": {"type": alert_type, "variant": "tonal",
                                                          "text": text}},
                    ],
                },
            ],
        }
