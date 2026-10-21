# MFA Token 过期与 MFA 重置说明

## 症状
- VPN / SSO 登录时 MFA 验证一直失败，提示"验证码无效"或"token 已过期"
- Authenticator 应用重新安装后扫描二维码提示"账号已绑定"
- 更换手机后无法完成 MFA 验证

## 诊断步骤
1. 确认现象属于 MFA 认证失败，而非密码错误或 VPN 客户端问题
2. 确认用户是否更换了手机或重装了 Authenticator
3. MFA token 有 90 天有效期，过期后必须由 IT 重置

## 解决步骤
- MFA 重置属于高敏感操作，Agent 不提供自助处理

## 何时升级人工（本场景一律升级）
- 所有 MFA 重置请求必须由 IT 人工核验身份后执行（策略：mfa_reset → human_verification）
- 这是为了防止攻击者通过对话渠道重置他人 MFA 从而接管账号
- Agent 可以做的是：确认现象、告知处理流程、创建带完整上下文的升级工单
