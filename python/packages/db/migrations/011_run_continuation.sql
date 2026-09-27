-- 失败/中断后由「继续对话」发起的 run 标记为 continuation。
-- 这类 run 的 user_message 是服务端合成的内部续跑指令（不来自用户输入），
-- 历史记录不渲染对应的用户气泡，避免会话里出现一条用户没说过的“继续”。

ALTER TABLE agent_runs ADD COLUMN continuation boolean NOT NULL DEFAULT false;
