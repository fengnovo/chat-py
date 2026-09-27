import type {
  UploadOptions,
  UploadPhase,
  UploadTarget,
  WorkerInboundMessage,
  WorkerOutboundMessage,
} from './upload-pipeline';

// 注意：Worker 的 new URL 引用放在本模块而不是 upload-pipeline 中。
// upload-worker 会反向 import upload-pipeline（requestInit/requestComplete），
// 若 pipeline 再引用 worker URL 就会形成循环模块图，导致 Turbopack 构建崩溃。

export interface UploadWithWorkerResult {
  result: unknown;
  instant: boolean;
  compressed: boolean;
}

/** 起一个临时 Worker 跑完整个上传流程，结束后自动回收。 */
export function uploadFileWithWorker(input: {
  file: File;
  target: UploadTarget;
  options: UploadOptions;
  contentType?: string;
  onProgress?: (phase: UploadPhase, progress: number) => void;
}): Promise<UploadWithWorkerResult> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(
      new URL('../../workers/upload-worker.ts', import.meta.url),
      { type: 'module' },
    );
    const localId = crypto.randomUUID();
    const settle = (action: () => void) => {
      worker.terminate();
      action();
    };
    worker.onmessage = (event: MessageEvent<WorkerOutboundMessage>) => {
      const message = event.data;
      if (message.localId !== localId) return;
      switch (message.type) {
        case 'progress':
          input.onProgress?.(message.phase, message.progress);
          break;
        case 'success':
          settle(() =>
            resolve({
              result: message.result,
              instant: message.instant,
              compressed: message.compressed,
            }),
          );
          break;
        case 'error':
          settle(() => reject(new Error(message.message)));
          break;
        default:
          break;
      }
    };
    worker.onerror = () => settle(() => reject(new Error('上传 worker 执行失败')));
    const outbound: WorkerInboundMessage = {
      type: 'upload',
      localId,
      file: input.file,
      options: input.options,
      target: input.target,
      ...(input.contentType ? { contentType: input.contentType } : {}),
    };
    worker.postMessage(outbound);
  });
}
