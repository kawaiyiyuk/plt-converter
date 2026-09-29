# plt-converter 开发入口

- 产品规则从[缝纫记忆产品索引](https://github.com/kawaiyiyuk/fengrenjiyi-product-docs/blob/main/products/fengrenjiyi/INDEX.md)进入，读取文件转换等对应模块文档；同级本地仓库可用 `../product-docs/products/fengrenjiyi/INDEX.md`。用户可感知的额度、限制、状态与失败结果不能由实现静默决定。
- 跨仓任务读[工程协作流程](https://github.com/kawaiyiyuk/fengrenjiyi-product-docs/blob/main/engineering/README.md)，涉及多仓或新产品规则时使用其中的任务卡模板。当前代码范围为 `wx_app`、`wx-server`、`web`、`plt-converter`；`ios_app` 暂不参与。
- 五仓按[工程协作角色规则](../product-docs/engineering/README.md#二五仓-agent-角色与交接)执行：`gpt-6-astra` 主 Agent 制定架构与任务规格，主动创建并委派 `gpt-6.1-sol` 子 Agent 具体开发、自检及整理资料和文档；Sol 回报改动与证据，Astra 独立 Review／验收并汇总最终报告。用户无需重复要求创建子 Agent 或切换主会话模型；冲突的旧角色与模型路由不适用，重要产品取舍由用户决定。
- 本仓是独立的 Flask + Redis/RQ 转换服务。先从 [README](README.md) 核对路由、队列和现有接口，再读相关 `app/`、`worker.py`、`tests/`；接口、计费或回调变化应同时核对 `wx-server` 和实际客户端调用。
- 验证命令见 [README 的验证段](README.md#验证)，按改动选择并报告实际环境、命令与结果。脚本或单元测试通过不能替代真实文件、队列、目标环境或真机验收。
- 保护各仓未提交工作。没有当前任务对生产环境的明确授权，不读取或操作生产环境；合并代码不代表部署。
