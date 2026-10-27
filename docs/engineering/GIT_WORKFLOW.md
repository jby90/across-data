# Git 与远端协作约定

## 当前仓库

- 最新项目目录：`E:\汽研项目\CrossBorderPlatform`。
- GitHub：<https://github.com/jby90/across-data>，当前为公开仓库。
- 当前开发分支：`codex/user-experience-legal-terms`。
- `E:\AutoDataCross` 是另外一个旧项目，关联 `jby90/AutoDataCross`；其未提交修改不并入本仓库。

2026-10-26 同步前，当前分支比远端 `main` 多 97 个提交，包含此前的修复与近期用户体验改进。本次保留分段历史，通过独立分支和草稿 PR 集成，不直接更新 `main`，不改写已存在的提交。

## 日常开发

在当前开发分支工作，先检查工作区，再按功能提交：

```powershell
git status --short --branch
git fetch origin --prune
git pull --ff-only
# 修改完成，运行相应回归后，明确选择要提交的文件
git add <文件路径>
git commit -m "说明本阶段完成的内容"
git push
```

工作区有修改或分支出现分叉时先检查差异，不使用 `reset --hard` 或强推清理。合并前保留原有测试、来源保护和人工复核门槛。

## 同事开发第二版本

同事使用单独目录克隆，再建立自己的分支：

```powershell
git clone --branch codex/user-experience-legal-terms https://github.com/jby90/across-data.git colleague-v2
cd colleague-v2
git switch -c codex/colleague-v2
# 完成并验证改动后
git push -u origin codex/colleague-v2
```

同事的数据库、原文归档目录、运行端口和环境变量也需独立。Git 分支只隔离代码，不隔离数据库或服务器；不给同事的开发环境直接连接生产数据库。不把真实 `.env`、数据库备份、账号口令、SSH 私钥或模型密钥加入仓库。需要分享业务数据时，通过另行受控的备份交接。

## 发布与回退

- 当前线上应用源码为 `b95754f`，发布标签 `deploy-20261026-chat-feedback` 指向该提交；后续验证文档提交可以更新开发分支，但不代表线上应用已经重新部署。
- `a2853d1`：真实业务阶段反馈与有边界的 PDF 缓存。
- `b95754f`：等待气泡、对话状态点、新动态和导出提示。
- `a64d000`：线上验证与原文、证据和审批保护记录。
- 当前验证：后端 591 项、前端 42 项、实时事件检查 5 项通过；人工法律核验仍按原状态保留。

后续发布使用新标签固定实际部署的源码提交，不覆盖旧标签。代码回退与数据恢复分开操作；回退代码不自动删除或覆盖原文、证据、报告版本和审批记录。

部署详情见 [聊天反馈与导出验证](../reviews/CHAT_FEEDBACK_EXPORT_20261026.md)。
