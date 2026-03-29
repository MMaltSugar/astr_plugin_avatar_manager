import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, cast
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools  # 导入StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api import FunctionTool

# ===================== 重构后的数据模型（完全符合要求） =====================
@dataclass
class AvatarOutfit:
    """单套装扮模型：仅包含描述和词条，无冗余字段"""

    description: str = field(default="无简介")  # 50字内简介
    fields: Dict[str, str] = field(default_factory=dict)  # 形象词条键值对

    def __post_init__(self):
        """自动限制简介长度不超过50字"""
        if len(self.description) > 50:
            self.description = self.description[:47] + "..."


@dataclass
class ConversationAvatar:
    """对话级形象总模型，完全符合你要求的结构"""

    conversation_id: str  # 对话唯一ID
    current_outfit: str = "常服"  # 当前形象：仅存着装名（指针），指向outfits中的键
    outfits: Dict[str, AvatarOutfit] = field(
        default_factory=dict
    )  # 形象列表：所有装扮统一存放


# ===================== 核心：对话ID获取方法（稳定兼容） =====================
def _get_conversation_id(event: AstrMessageEvent) -> str:
    """从消息事件中获取当前对话的唯一ID，兼容AstrBot全版本"""

    def _sid_from_event(ev: AstrMessageEvent) -> Optional[str]:
        if ev is None:
            return None
        if hasattr(ev, "session_id"):
            sid = getattr(ev, "session_id", None)
            if sid is not None:
                return str(sid)
        if hasattr(ev, "get_session_id"):
            sid = ev.get_session_id()
            if sid is not None:
                return str(sid)
        if hasattr(ev, "message_obj"):
            mobj = getattr(ev, "message_obj", None)
            if mobj and hasattr(mobj, "session_id"):
                return str(getattr(mobj, "session_id"))
        if hasattr(ev, "unified_msg_origin"):
            umo = getattr(ev, "unified_msg_origin", None)
            if umo:
                return str(umo)
        return None

    sid = _sid_from_event(event)
    if sid:
        safe_sid = "".join(c if c.isalnum() or c in "-_:" else "_" for c in sid)
        logger.debug(f"获取到当前对话ID: {safe_sid}")
        return safe_sid

    fallback_sid = f"fallback_conv_{os.urandom(4).hex()}"
    logger.warning(f"无法获取对话ID，使用兜底ID: {fallback_sid}")
    return fallback_sid


# ===================== LLM函数工具定义（移除全局变量，接收插件实例） =====================
@dataclass
class CreateAvatarOutfitTool(FunctionTool):
    name: str = "create_avatar_outfit"
    description: str = "创建/覆盖形象列表中的指定着装，支持自定义词条和简介。【规则】：修改4条及以上词条，直接调用本工具覆写对应着装"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)  # 持有插件实例
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "outfit_name": {
                    "type": "string",
                    "description": "着装名称（如：常服、泳装、礼服）",
                },
                "description": {
                    "type": "string",
                    "description": "可选，50字内的着装简介，说明风格/适用场景",
                },
                "fields": {
                    "type": "object",
                    "description": '形象词条键值对，示例：{"发色":"粉色","上衣":"水手服"}',
                },
            },
            "required": ["outfit_name", "fields"],
        }
    )

    async def run(
        self,
        event: AstrMessageEvent,
        outfit_name: str,
        fields: Dict[str, str],
        description: str = "无简介",
    ):
        # 直接使用持有的插件实例，不再依赖全局变量
        conversation_id = _get_conversation_id(event)

        # 校验配置允许的词条
        config_fields = self.plugin_instance.config["avatar_fields"].split(",")
        if not self.plugin_instance.config.get("allow_custom_fields", True):
            fields = {k: v for k, v in fields.items() if k in config_fields}

        # 创建/覆写形象列表中的对应着装
        outfit = AvatarOutfit(description=description, fields=fields)
        self.plugin_instance.save_outfit_to_list(conversation_id, outfit_name, outfit)

        # 自动设置为当前形象（如果是首次创建）
        avatar_data = self.plugin_instance.load_conversation_avatar(conversation_id)
        if avatar_data and len(avatar_data.outfits) == 1:
            avatar_data.current_outfit = outfit_name
            self.plugin_instance.save_conversation_avatar(avatar_data)

        return f"✅ 成功在形象列表中创建/覆盖[{outfit_name}]\n简介：{outfit.description}\n形象词条：{fields}"


@dataclass
class SelectAvatarOutfitTool(FunctionTool):
    name: str = "select_avatar_outfit"
    description: str = "切换当前形象，从形象列表中选择指定着装设为当前使用的形象，无需覆写任何数据，仅切换引用"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)  # 持有插件实例
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "outfit_name": {
                    "type": "string",
                    "description": "要切换的着装名称（必须是形象列表中已有的）",
                },
            },
            "required": ["outfit_name"],
        }
    )

    async def run(self, event: AstrMessageEvent, outfit_name: str):
        conversation_id = _get_conversation_id(event)

        avatar_data = self.plugin_instance.load_conversation_avatar(conversation_id)
        if not avatar_data:
            return f"❌ 错误：当前对话暂无形象数据"
        if outfit_name not in avatar_data.outfits:
            return f"❌ 错误：形象列表中无[{outfit_name}]\n当前可用形象：{list(avatar_data.outfits.keys())}"

        # 仅修改当前形象指针
        avatar_data.current_outfit = outfit_name
        self.plugin_instance.save_conversation_avatar(avatar_data)

        # 返回切换后的形象详情
        current_outfit = avatar_data.outfits[outfit_name]
        return f"✅ 成功切换当前形象为[{outfit_name}]\n简介：{current_outfit.description}\n形象词条：{current_outfit.fields}"


@dataclass
class ModifyAvatarFieldTool(FunctionTool):
    name: str = "modify_avatar_field"
    description: str = "修改形象列表中指定着装的单个词条或简介。【规则】：仅用于修改1-3条内容，4条及以上请用create_avatar_outfit覆写"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)  # 持有插件实例
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "outfit_name": {
                    "type": "string",
                    "description": "要修改的着装名称（如：常服）",
                },
                "field_name": {
                    "type": "string",
                    "description": "要修改的词条名，修改简介请填「description」",
                },
                "field_value": {
                    "type": "string",
                    "description": "修改后的新值（简介请控制在50字内）",
                },
            },
            "required": ["outfit_name", "field_name", "field_value"],
        }
    )

    async def run(
        self,
        event: AstrMessageEvent,
        outfit_name: str,
        field_name: str,
        field_value: str,
    ):
        conversation_id = _get_conversation_id(event)

        avatar_data = self.plugin_instance.load_conversation_avatar(conversation_id)
        if not avatar_data:
            return f"❌ 错误：当前对话暂无形象数据"
        if outfit_name not in avatar_data.outfits:
            return f"❌ 错误：形象列表中无[{outfit_name}]\n可用形象：{list(avatar_data.outfits.keys())}"

        # 修改简介
        if field_name == "description":
            if len(field_value) > 50:
                field_value = field_value[:47] + "..."
            avatar_data.outfits[outfit_name].description = field_value
            self.plugin_instance.save_conversation_avatar(avatar_data)
            return f"✅ 成功修改[{outfit_name}]的简介\n新简介：{field_value}"

        # 修改形象词条
        avatar_data.outfits[outfit_name].fields[field_name] = field_value
        self.plugin_instance.save_conversation_avatar(avatar_data)
        return f"✅ 成功修改[{outfit_name}]的形象词条\n{field_name} → {field_value}"


@dataclass
class DeleteAvatarOutfitTool(FunctionTool):
    name: str = "delete_avatar_outfit"
    description: str = "从形象列表中删除指定着装，【限制】：无法删除当前正在使用的形象"
    plugin_instance: "BotAvatarManager" = field(default=None, repr=False)  # 持有插件实例
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "outfit_name": {"type": "string", "description": "要删除的着装名称"},
            },
            "required": ["outfit_name"],
        }
    )

    async def run(self, event: AstrMessageEvent, outfit_name: str):
        conversation_id = _get_conversation_id(event)

        avatar_data = self.plugin_instance.load_conversation_avatar(conversation_id)
        if not avatar_data:
            return f"❌ 错误：当前对话暂无形象数据"
        if outfit_name not in avatar_data.outfits:
            return f"❌ 错误：形象列表中无[{outfit_name}]\n可用形象：{list(avatar_data.outfits.keys())}"

        # 安全校验：禁止删除当前正在使用的形象
        if avatar_data.current_outfit == outfit_name:
            return f"❌ 错误：无法删除当前正在使用的[{outfit_name}]！请先切换到其他形象后再删除"

        # 执行删除
        del avatar_data.outfits[outfit_name]
        self.plugin_instance.save_conversation_avatar(avatar_data)
        return f"✅ 成功从形象列表中删除[{outfit_name}]\n剩余可用形象：{list(avatar_data.outfits.keys())}"


# ===================== 插件主类 =====================
class BotAvatarManager(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 修复1：使用StarTools获取规范数据目录（pathlib方式）
        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 修复3：注册LLM工具时传入插件实例，不再使用全局变量
        self.context.add_llm_tools(
            CreateAvatarOutfitTool(plugin_instance=self),
            SelectAvatarOutfitTool(plugin_instance=self),
            ModifyAvatarFieldTool(plugin_instance=self),
            DeleteAvatarOutfitTool(plugin_instance=self),
        )
        logger.info("=====[avatar_manager]init=====")

    # --------------------- 事件监听器：自动插入形象数据到上下文 ---------------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        #LLM请求前钩子：无形象自动创建默认两套，插入当前形象+形象列表到上下文
        logger.info("监听到对话，开始插入形象")
        conversation_id = _get_conversation_id(event)
        avatar_data = self.load_conversation_avatar(conversation_id)

        # 无形象时自动创建两套默认形象
        if not avatar_data or len(avatar_data.outfits) == 0:
            # 1. 常服（默认当前形象）
            normal_outfit = AvatarOutfit(
                description="日常校园通勤穿搭，正式得体",
                fields={
                    "上衣": "白色衬衫+灰色马甲",
                    "下着": "黑色百褶裙",
                    "袜子": "白色裤袜",
                    "鞋子": "棕色小皮鞋",
                    "内衣": "蓝白条内衣",
                    "内裤": "蓝白条内裤",
                },
            )
            # 2. 居家服
            home_outfit = AvatarOutfit(
                description="舒适居家休闲穿搭，柔软亲肤",
                fields={
                    "上衣": "白色纱质连衣裙",
                    "内衣": "黑色蕾丝内衣",
                    "内裤": "黑色蕾丝内裤",
                },
            )
            # 保存到形象列表
            self.save_outfit_to_list(conversation_id, "常服", normal_outfit)
            self.save_outfit_to_list(conversation_id, "居家服", home_outfit)
            # 重新加载数据
            avatar_data = self.load_conversation_avatar(conversation_id)
            logger.info(f"为对话[{conversation_id}]自动创建两套默认形象：常服+居家服")

        if not avatar_data or avatar_data.current_outfit not in avatar_data.outfits:
            logger.info(f"对话[{conversation_id}]无有效形象数据，跳过LLM上下文插入")
            return

        # 1. 插入当前形象详细说明（完全符合结构）
        current_outfit_name = avatar_data.current_outfit
        current_outfit = avatar_data.outfits[current_outfit_name]
        avatar_text = "\n【你当前的形象设定（必须严格遵守）】\n"
        avatar_text += f"当前对话ID：{conversation_id}\n"
        avatar_text += f"当前形象：【{current_outfit_name}】\n"
        avatar_text += f"形象简介：{current_outfit.description}\n"
        avatar_text += "形象属性：\n"
        for field, value in current_outfit.fields.items():
            avatar_text += f"- {field}：{value}\n"
        avatar_text += "【当前形象设定结束】\n"

        # 2. 插入形象列表（所有可选形象的名字+简介）
        avatar_text += "\n【可用形象列表（你可根据场景自主切换）】\n"
        for outfit_name, outfit in avatar_data.outfits.items():
            avatar_text += f"- 【{outfit_name}】：{outfit.description}\n"
        avatar_text += "【可用形象列表结束】\n"

        # 按配置插入到指定位置
        insert_pos = cast(Optional[str], self.config.get("llm_insert_position"))
        if not insert_pos:
            insert_pos = "system_prompt_end"

        if insert_pos == "system_prompt_start":
            req.system_prompt = avatar_text + req.system_prompt
        elif insert_pos == "system_prompt_end":
            req.system_prompt += avatar_text
        elif insert_pos == "user_prompt_start":
            req.prompt = avatar_text + req.prompt
        elif insert_pos == "user_prompt_end":
            req.prompt += avatar_text

        logger.info(f"已将对话[{conversation_id}]的形象数据插入到LLM上下文")

    # --------------------- 用户指令（适配新结构） ---------------------
    @filter.command("查看bot形象")
    async def view_avatar(self, event: AstrMessageEvent):
        """查看当前对话的所有形象数据，包含当前形象和完整形象列表"""
        conversation_id = _get_conversation_id(event)
        avatar_data = self.load_conversation_avatar(conversation_id)

        if not avatar_data:
            yield event.plain_result(f"❌ 当前对话暂无形象数据")
            return

        # 构建回复，完全符合结构展示
        reply_text = f"📝 当前对话Bot形象信息\n对话ID：{conversation_id}\n"
        reply_text += f"\n▶️ 当前形象：【{avatar_data.current_outfit}】\n"

        # 当前形象详情
        current_outfit = avatar_data.outfits[avatar_data.current_outfit]
        reply_text += f"简介：{current_outfit.description}\n"
        reply_text += "形象属性：\n"
        for field, value in current_outfit.fields.items():
            reply_text += f"- {field}：{value}\n"

        # 完整形象列表
        reply_text += f"\n📋 完整形象列表（共{len(avatar_data.outfits)}套）：\n"
        for outfit_name, outfit in avatar_data.outfits.items():
            reply_text += f"\n├─ 【{outfit_name}】（简介：{outfit.description}）\n"
            for field, value in outfit.fields.items():
                reply_text += f"│  └─ {field}：{value}\n"

        yield event.plain_result(reply_text)

    @filter.command("创建bot形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def create_outfit_admin(
        self,
        event: AstrMessageEvent,
        outfit_name: str,
        description: str = "无简介",
        *args,  # 修复4：使用*args接收剩余参数，手动解析等号分隔的字段
    ):
        """管理员创建形象，示例：创建bot形象 泳装 海边度假穿搭 上衣=粉色比基尼 下着=粉色比基尼"""
        conversation_id = _get_conversation_id(event)

        # 修复4：手动解析等号分隔的字段参数
        outfit_fields = {}
        for arg in args:
            if "=" in arg:
                key, value = arg.split("=", 1)  # 只分割第一个等号，避免值中包含等号
                outfit_fields[key.strip()] = value.strip()

        # 校验配置允许的词条
        config_fields = cast(Optional[str], self.config.get("avatar_fields"))
        if config_fields:
            config_fields_list = config_fields.split(",")
            outfit_fields = {k: v for k, v in outfit_fields.items() if k in config_fields_list}

        outfit = AvatarOutfit(description=description, fields=outfit_fields)
        self.save_outfit_to_list(conversation_id, outfit_name, outfit)

        yield event.plain_result(
            f"✅ 成功在形象列表中创建[{outfit_name}]\n简介：{outfit.description}\n词条：{outfit_fields}"
        )

    @filter.command("切换bot形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def switch_outfit_admin(self, event: AstrMessageEvent, outfit_name: str):
        """管理员切换当前形象，示例：切换bot形象 居家服"""
        conversation_id = _get_conversation_id(event)

        avatar_data = self.load_conversation_avatar(conversation_id)
        if not avatar_data:
            yield event.plain_result(f"❌ 当前对话暂无形象数据")
            return
        if outfit_name not in avatar_data.outfits:
            yield event.plain_result(
                f"❌ 形象列表中无[{outfit_name}]\n可用形象：{list(avatar_data.outfits.keys())}"
            )
            return

        avatar_data.current_outfit = outfit_name
        self.save_conversation_avatar(avatar_data)
        yield event.plain_result(f"✅ 成功切换当前形象为【{outfit_name}】")

    @filter.command("删除bot形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def delete_outfit_admin(self, event: AstrMessageEvent, outfit_name: str):
        """管理员删除形象，示例：删除bot形象 旧泳装"""
        conversation_id = _get_conversation_id(event)

        avatar_data = self.load_conversation_avatar(conversation_id)
        if not avatar_data:
            yield event.plain_result(f"❌ 当前对话暂无形象数据")
            return
        if outfit_name not in avatar_data.outfits:
            yield event.plain_result(f"❌ 形象列表中无[{outfit_name}]")
            return
        if avatar_data.current_outfit == outfit_name:
            yield event.plain_result(f"❌ 无法删除当前正在使用的形象，请先切换后再删除")
            return

        del avatar_data.outfits[outfit_name]
        self.save_conversation_avatar(avatar_data)
        yield event.plain_result(
            f"✅ 成功删除形象【{outfit_name}】\n剩余形象：{list(avatar_data.outfits.keys())}"
        )

    @filter.command("清空当前对话形象")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def clear_conversation_avatar(self, event: AstrMessageEvent):
        """清空当前对话的所有形象数据"""
        conversation_id = _get_conversation_id(event)
        file_path = self.get_conversation_file_path(conversation_id)

        if os.path.exists(file_path):
            os.remove(file_path)
            yield event.plain_result(
                f"✅ 已清空当前对话[{conversation_id}]的所有形象数据"
            )
        else:
            yield event.plain_result(f"❌ 当前对话暂无形象数据")

    # --------------------- 数据读写核心方法（适配新结构+旧数据兼容） ---------------------
    def get_conversation_file_path(self, conversation_id: str) -> str:
        """获取对话形象数据文件路径（修复1：pathlib拼接）"""
        return str(self.data_dir / f"{conversation_id}.json")

    def load_conversation_avatar(
        self, conversation_id: str
    ) -> Optional[ConversationAvatar]:
        """加载对话形象数据，自动兼容旧版本数据（修复2：异常时备份损坏文件）"""
        file_path = self.get_conversation_file_path(conversation_id)
        if not os.path.exists(file_path):
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # ========== 旧数据自动迁移 ==========
            # 检测到旧版本结构（outfits里有「当前形象」）
            if isinstance(data.get("outfits"), list):
                logger.info(
                    f"检测到对话[{conversation_id}]的旧版本数据，自动迁移到新结构"
                )
                old_outfits = data.get("outfits", [])
                new_outfits = {}
                current_outfit_name = "常服"

                # 遍历旧数据，转换为新结构
                for o in old_outfits:
                    name = o.get("outfit_name", "未知")
                    desc = o.get("description", "无简介")
                    fields = o.get("fields", {})

                    if name == "当前形象":
                        # 旧的当前形象，自动合并到常服
                        if "常服" not in new_outfits:
                            new_outfits["常服"] = AvatarOutfit(
                                description=desc, fields=fields
                            )
                            current_outfit_name = "常服"
                    else:
                        new_outfits[name] = AvatarOutfit(
                            description=desc, fields=fields
                        )

                # 构建新结构
                return ConversationAvatar(
                    conversation_id=conversation_id,
                    current_outfit=current_outfit_name,
                    outfits=new_outfits,
                )
            # =====================================

            # 新版本结构正常加载
            outfits = {}
            for name, outfit_data in data.get("outfits", {}).items():
                outfits[name] = AvatarOutfit(
                    description=outfit_data.get("description", "无简介"),
                    fields=outfit_data.get("fields", {}),
                )

            return ConversationAvatar(
                conversation_id=data.get("conversation_id", conversation_id),
                current_outfit=data.get("current_outfit", "常服"),
                outfits=outfits,
            )
        except Exception as e:
            # 修复2：备份损坏的文件，避免数据丢失
            logger.error(f"加载对话[{conversation_id}]形象数据失败：{e}")
            backup_path = f"{file_path}.bak.{os.urandom(4).hex()}"
            os.rename(file_path, backup_path)
            logger.warning(f"已将损坏的文件备份至：{backup_path}")
            return None

    def save_conversation_avatar(self, avatar_data: ConversationAvatar):
        """保存对话形象数据到文件"""
        file_path = self.get_conversation_file_path(avatar_data.conversation_id)
        try:
            data = {
                "conversation_id": avatar_data.conversation_id,
                "current_outfit": avatar_data.current_outfit,
                "outfits": {
                    name: asdict(outfit) for name, outfit in avatar_data.outfits.items()
                },
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"成功保存对话[{avatar_data.conversation_id}]的形象数据")
        except Exception as e:
            logger.error(f"保存对话[{avatar_data.conversation_id}]形象数据失败：{e}")

    def save_outfit_to_list(
        self, conversation_id: str, outfit_name: str, outfit: AvatarOutfit
    ):
        """便捷方法：添加/覆写形象列表中的指定着装"""
        avatar_data = self.load_conversation_avatar(conversation_id)
        if not avatar_data:
            avatar_data = ConversationAvatar(conversation_id=conversation_id)

        avatar_data.outfits[outfit_name] = outfit
        self.save_conversation_avatar(avatar_data)

    async def terminate(self):
        """插件卸载时清理"""
        logger.info("对话级Bot形象管理插件已安全卸载")