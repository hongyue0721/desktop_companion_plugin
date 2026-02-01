# 桌面伴侣插件 - 项目结构

```
desktop_companion_plugin/
├── _manifest.json          # 插件清单文件（MaiBot 插件规范必需）
├── plugin.py               # 插件主逻辑入口
├── config.example.toml     # 配置文件示例
├── requirements.txt        # Python 依赖声明
├── README.md               # 使用说明文档
├── STRUCTURE.md            # 本文件 - 项目结构说明
└── LICENSE                 # MIT 开源许可证
```

## 文件说明

### _manifest.json
MaiBot 插件系统必需的清单文件，声明：
- `manifest_version`: 清单版本号
- `name`: 插件显示名称
- `version`: 插件版本号
- `python_dependencies`: Python 依赖包列表

### plugin.py
插件核心实现，包含：
- `DesktopCompanionPlugin`: 主插件类，继承 `BasePlugin`
- `DesktopEvent`: Peewee 数据模型，存储日程事件
- `AddEventCommand`: 添加日程命令处理器
- `ListEventsCommand`: 查询日程命令处理器
- 后台任务：事件提醒、每日提醒、截图循环

### config.example.toml
配置文件模板，包含：
- `[plugin]`: 插件启用开关
- `[schedule]`: 日程检查间隔配置
- `[screenshot]`: 截图功能配置
- `[target]`: 消息目标配置

## 依赖关系

```
MaiBot Core
    └── desktop_companion_plugin (本插件)
            ├── 依赖 maibot.plugin 模块
            ├── 依赖 maim_message 消息库
            ├── 依赖 peewee ORM
            └── 依赖 pyautogui 截图库
```

## 数据流

```
用户命令 → MaiCore → 插件命令处理器 → 数据库操作/截图
                                          ↓
后台任务 ← 定时触发 ← asyncio 事件循环
    ↓
提醒消息 → MaiCore → 桌面适配器 → 桌面 UI
```
