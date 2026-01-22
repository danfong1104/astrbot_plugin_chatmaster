import json
import time
import asyncio
from datetime import datetime
from astrbot.api.all import *
from astrbot.api import logger  # 1. 修复：使用官方标准的日志工具
from astrbot.api.star import StarTools # 2. 修复：使用官方数据目录管理
from astrbot.api.event import filter

@register("astrbot_plugin_chatmaster", "ChatMaster", "活跃度监控插件", "1.3.0")
class ChatMasterPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_changed = False # 标记数据是否发生变化
        
        # 3. 修复：使用 StarTools 获取规范的数据存储路径
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_chatmaster")
        self.data_file = self.data_dir / "data.json" # Path对象拼接
        
        self.data = self.load_data()
        
        # 4. 优化：预处理白名单，将列表转换为字典，极大提升查询速度 (O(N) -> O(1))
        self.nickname_cache = {}
        self.refresh_nickname_cache()
        
        # 5. 修复：保存任务引用，防止变成“幽灵任务”
        self.scheduler_task = asyncio.create_task(self.scheduler_loop())

    def refresh_nickname_cache(self):
        """将配置的列表转换为字典，方便快速查找"""
        mapping = {}
        raw_list = self.config.get("nickname_mapping", [])
        if raw_list:
            for item in raw_list:
                # 增强健壮性：处理可能的格式问题
                item_str = str(item).replace("：", ":")
                if ":" in item_str:
                    parts = item_str.split(":", 1)
                    if len(parts) == 2:
                        qq = parts[0].strip()
                        name = parts[1].strip()
                        mapping[qq] = name
        self.nickname_cache = mapping

    def load_data(self):
        """加载数据"""
        default_data = {"global_last_run_date": "", "groups": {}}
        
        # 检查文件是否存在
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
        """保存数据到磁盘"""
        # 性能优化：只有数据确实改变了才写入磁盘
        if not self.data_changed:
            return

        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            self.data_changed = False # 重置标记
            # logger.debug("ChatMaster 数据已保存") 
        except Exception as e:
            logger.error(f"ChatMaster 保存数据失败: {e}")

    def terminate(self):
        """生命周期钩子：插件卸载/关闭时调用"""
        # 取消后台任务
        if self.scheduler_task:
            self.scheduler_task.cancel()
        # 强制保存一次数据
        self.save_data()
        logger.info("ChatMaster 插件已停止，数据已保存。")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        """监听消息：仅更新内存数据，不写硬盘"""
        message_obj = event.message_obj
        if not message_obj.group_id:
            return

        group_id = str(message_obj.group_id)
        user_id = str(message_obj.sender.user_id)
        
        # 检查群
        monitored_groups = self.config.get("monitored_groups", [])
        if monitored_groups and group_id not in monitored_groups:
            return

        # 优化：O(1) 极速检查用户是否在白名单
        if user_id not in self.nickname_cache:
            return 

        if group_id not in self.data["groups"]:
            self.data["groups"][group_id] = {}

        # 仅更新内存中的时间戳
        self.data["groups"][group_id][user_id] = time.time()
        # 标记数据已变脏，等待定时任务去保存
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
            # 使用缓存的字典直接获取昵称
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
        """后台调度循环"""
        while True:
            try:
                # 1. 检查推送时间
                await self.check_schedule()
                
                # 2. 定期保存数据 (每分钟检查一次是否需要保存)
                # 这样既保证了数据安全，又避免了高频IO
                self.save_data()
                
            except asyncio.CancelledError:
                # 任务被取消时退出循环
                break
            except Exception as e:
                logger.error(f"ChatMaster 调度出错: {e}")
            
            await asyncio.sleep(60)

    async def check_schedule(self):
        push_time_str = self.config.get("push_time", "09:00")
        
        now = datetime.now()
        today_date_str = now.strftime("%Y-%m-%d")
        
        try:
            target_h, target_m = map(int, push_time_str.split(':'))
        except:
            target_h, target_m = 9, 0

        is_time_up = (now.hour > target_h) or (now.hour == target_h and now.minute >= target_m)
        last_run = self.data.get("global_last_run_date", "")
        
        if is_time_up and last_run != today_date_str:
            logger.info(f"ChatMaster: 到达设定时间 {push_time_str}，触发每日检测...")
            await self.run_inspection()
            self.data["global_last_run_date"] = today_date_str
            self.data_changed = True # 标记需要保存
            self.save_data() # 立即保存一次状态

    async def run_inspection(self):
        monitored_groups = self.config.get("monitored_groups", [])
        timeout_days_cfg = float(self.config.get("timeout_days", 1.0))
        timeout_seconds = timeout_days_cfg * 24 * 3600
        template = self.config.get("alert_template", "“{nickname}”已经“{days}”天没发言了")
        now_ts = time.time()

        logger.info(f"ChatMaster: === 开始执行活跃度检测 (阈值: {timeout_days_cfg}天) ===")

        for group_id in monitored_groups:
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
                await asyncio.sleep(2)
            else:
                logger.info(f"ChatMaster: -> 群 {group_id} 结果: 无需推送。")
