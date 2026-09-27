export type ChatAttachmentKind = 'image' | 'text' | 'file';

/** 与后端 attachmentHistoryView 对齐的附件元数据。 */
export type ChatAttachmentView = {
  id: string;
  filename: string;
  contentType: string;
  sizeBytes: number;
  kind: ChatAttachmentKind;
  /** 鉴权重定向地址：浏览器取内容时由后端换发新鲜预签名 URL。 */
  url: string;
};

/** 删除尚未发送（未关联 run）的附件；已发送的附件后端会拒绝。 */
export async function deleteChatAttachment(attachmentId: string): Promise<void> {
  const response = await fetch(
    `/api/agent/chat-attachments/${encodeURIComponent(attachmentId)}`,
    { method: 'DELETE' },
  );
  if (!response.ok && response.status !== 404) {
    throw new Error(`附件删除失败（HTTP ${response.status}）`);
  }
}
