import json
import time
import asyncio
from datetime import datetime
# 1. 修复命名空间污染：显式导入所需类，符合官方规范
from astrbot.api.all import Context, AstrMessageEvent, Star, register
from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.api.event import filter

@register("astrbot_plugin_chatmaster", "ChatMaster", "活跃度监控插件", "1.3.0")
class ChatMasterPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_changed = False 
        
        # 使用官方工具获取路径
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_chatmaster")
        self.data_file = self.data_dir / "data.json"
        
        self.data = self.load_data()
        
        self.nickname_cache = {}
        self.refresh_nickname_cache()

        # 5. 修复时间解析脆弱性：初始化时就验证并解析时间
        self.push_time_h, self.push_time_m = self._parse_push_time()
        
        self.scheduler_task = asyncio.create_task(self.scheduler_loop())

    def _parse_push_time(self):
        """解析并验证推送时间，增加健壮性"""
        push_time_str = self.config.get("push_time", "09:00")
        # 兼容中文冒号
        push_time_str = push_time_str.replace("：", ":")
        try:
            h, m = map(int, push_time_str.split(':'))
            if 0 <= h < 24 and 0 <= m < 60:
                return h, m
            else:
                raise ValueError("时间数值越界")
        except Exception as e:
            logger.error(f"ChatMaster 配置错误: 推送时间 '{push_time_str}' 格式不正确 ({e})。已重置为 09:00")
            return 9, 0

    def refresh_nickname_cache(self):
        """刷新昵称缓存"""
        mapping = {}
        raw_list = self.config.get("nickname_mapping", [])
        if raw_list:
            for item in raw_list:
                item_str = str(item).replace("：", ":")
                if ":" in item_str:
                    parts = item_str.split(":", 1)
                    if len(parts) == 2:
                        qq = parts[0].strip()
                        name = parts[1].strip()
                        mapping[qq] = name
        self.nickname_cache = mapping

    def load_data(self):
        default_data = {"global_last_run_date": "", "groups": {}}
        if not self.data_file.exists():
            return default_data
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if loaded and "global_last_run_date" not in loaded:
                    return {"global_last_run_date": "", "groups": loaded}
                return loaded
        except Exception as e:
            logger.error(f"ChatMaster 加载数据失败: {e}")
            return default_data

    def save_data(self):
        if not self.data_changed:
            return
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            self.data_changed = False
        except Exception as e:
            logger.error(f"ChatMaster 保存数据失败: {e}")

    def terminate(self):
        """生命周期管理：插件卸载时保存数据"""
        if self.scheduler_task:
            self.scheduler_task.cancel()
        self.save_data()
        logger.info("ChatMaster 插件已停止，数据已保存。")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        message_obj = event.message_obj
        if not message_obj.group_id:
            return

        group_id = str(message_obj.group_id)
        user_id = str(message_obj.sender.user_id)
        
        monitored_groups = self.config.get("monitored_groups", [])
        if monitored_groups and group_id not in monitored_groups:
            return

        # 保持你要求的白名单逻辑
        if user_id not in self.nickname_cache:
            return 

        if group_id not in self.data["groups"]:
            self.data["groups"][group_id] = {}

        self.data["groups"][group_id][user_id] = time.time()
        self.data_changed = True 

    @filter.command("聊天检测")
    async def manual_check(self, event: AstrMessageEvent):
        message_obj = event.message_obj
        if not message_obj.group_id:
            yield event.plain_result("🚫 请在群聊中使用此命令。")
            return

        group_id = str(message_obj.group_id)
        
        if group_id not in self.data["groups"] or not self.data["groups"][group_id]:
            yield event.plain_result(f"📭 群 ({group_id}) 暂无监控数据。")
            return

        group_data = self.data["groups"][group_id]
        msg_lines = [f"📊 群 ({group_id}) 活跃度数据概览："]
        
        now = time.time()
        count = 0
        
        for user_id, last_seen_ts in group_data.items():
            nickname = self.nickname_cache.get(user_id)
            if not nickname:
                continue
                
            last_seen_dt = datetime.fromtimestamp(last_seen_ts)
            last_seen_str = last_seen_dt.strftime('%Y-%m-%d %H:%M:%S')
            
            diff_seconds = now - last_seen_ts
            days = int(diff_seconds // 86400)
            
            status_emoji = "🟢" if days < 1 else "🔴"
            msg_lines.append(f"{status_emoji} {nickname} | 未发言: {days}天 | 最后: {last_seen_str}")
            count += 1

        msg_lines.append(f"\n共记录 {count} 人。")
        yield event.plain_result("\n".join(msg_lines))

    async def scheduler_loop(self):
        while True:
            try:
                await self.check_schedule()
                self.save_data()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"ChatMaster 调度出错: {e}")
            await asyncio.sleep(60)

    async def check_schedule(self):
        now = datetime.now()
        today_date_str = now.strftime("%Y-%m-%d")
        
        # 使用预解析的时间，更安全
        target_h, target_m = self.push_time_h, self.push_time_m

        is_time_up = (now.hour > target_h) or (now.hour == target_h and now.minute >= target_m)
        last_run = self.data.get("global_last_run_date", "")
        
        if is_time_up and last_run != today_date_str:
            logger.info(f"ChatMaster: 到达设定时间 {target_h:02d}:{target_m:02d}，触发每日检测...")
            
            # 2. 修复重试风暴的关键：先更新日期，再执行检测。
            # 这样即使 run_inspection 发生网络错误崩溃，今天也不会再尝试第二次，避免刷屏。
            self.data["global_last_run_date"] = today_date_str
            self.data_changed = True
            self.save_data() # 立即落盘，防止断电导致重复发送
            
            # 执行检测
            await self.run_inspection()

    async def run_inspection(self):
        monitored_groups = self.config.get("monitored_groups", [])
        timeout_days_cfg = float(self.config.get("timeout_days", 1.0))
        timeout_seconds = timeout_days_cfg * 24 * 3600
        template = self.config.get("alert_template", "“{nickname}”已经“{days}”天没发言了")
        now_ts = time.time()

        logger.info(f"ChatMaster: === 开始执行活跃度检测 (阈值: {timeout_days_cfg}天) ===")

        for group_id in monitored_groups:
            # 3. 修复异常粒度：使用 try-except 包裹每个群的逻辑
            # 确保一个群发送失败（如被禁言）不会影响其他群的发送
            try:
                group_id = str(group_id)
                group_data = self.data["groups"].get(group_id, {})
                
                if not group_data:
                    continue

                msg_list = []
                
                for user_id, last_seen_ts in group_data.items():
                    nickname = self.nickname_cache.get(user_id)
                    if not nickname:
                        continue
                    
                    time_diff = now_ts - last_seen_ts
                    
                    if time_diff >= timeout_seconds:
                        days_silent = int(time_diff // 86400)
                        last_seen_str = datetime.fromtimestamp(last_seen_ts).strftime('%Y-%m-%d %H:%M:%S')
                        
                        line = template.format(
                            nickname=nickname, 
                            days=days_silent, 
                            last_seen=last_seen_str
                        )
                        msg_list.append(line)
                        logger.info(f"ChatMaster:   -> 发现潜水员: {nickname} (未发言 {days_silent} 天)")
                
                if msg_list:
                    logger.info(f"ChatMaster: -> 群 {group_id} 结果: 需推送。共发现 {len(msg_list)} 人。")
                    final_msg = "\n".join(msg_list)
                    await self.context.send_message(
                        target_group_id=group_id, 
                        message_str=f"📢 潜水员日报：\n{final_msg}"
                    )
                    await asyncio.sleep(2) # 避免并发过快
                else:
                    logger.info(f"ChatMaster: -> 群 {group_id} 结果: 无需推送。")

            except Exception as e:
                # 记录错误但继续处理下一个群
                logger.error(f"ChatMaster: 处理群 {group_id} 时发生错误: {e}")
                continue
