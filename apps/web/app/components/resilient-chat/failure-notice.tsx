import { Icon } from './icon';
import type { TaskFailure } from './types';

function taskFailureCopy(failure: TaskFailure) {
  const detail = `${failure.code} ${failure.message}`.toLowerCase();
  if (
    detail.includes('402') ||
    detail.includes('quota') ||
    detail.includes('billing') ||
    detail.includes('trial') ||
    detail.includes('额度')
  ) {
    return {
      title: '模型服务暂时不可用',
      detail: '当前模型额度不足。你可以稍后再试，或联系管理员切换可用模型。',
    };
  }
  if (
    detail.includes('recursion') ||
    detail.includes('step limit') ||
    detail.includes('call limit')
  ) {
    return {
      title: '任务没有顺利收敛',
      detail:
        'Agent 执行步骤已达到上限，但已完成的文件和沙箱都保留着。点「继续对话」让它接着上次的进度做完即可。',
    };
  }
  if (
    detail.includes('sandbox') ||
    detail.includes('workspace') ||
    detail.includes('e2b')
  ) {
    return {
      title: '执行环境未能完成任务',
      detail: 'Sandbox 或工作区初始化没有成功。请稍后重新提交这条任务。',
    };
  }
  return {
    title: '本次任务未完成',
    detail: '连接仍然正常，你可以调整要求后继续发送消息。',
  };
}

function TaskFailureNotice({
  failure,
  onContinue,
}: {
  failure: TaskFailure;
  onContinue: () => void;
}) {
  const copy = taskFailureCopy(failure);
  return (
    <section className="task-failure-banner" role="status">
      <span className="task-failure-icon">
        <Icon name="triangle" size={18} />
      </span>
      <div>
        <strong>{copy.title}</strong>
        <p>{copy.detail}</p>
      </div>
      <button type="button" onClick={onContinue}>
        继续对话
      </button>
    </section>
  );
}

function friendlyError(error: Error) {
  if (error.message.includes('503')) {
    return 'Agent API 暂时不可用，请重新连接。';
  }
  if (error.message.includes('404')) {
    return '上次的运行记录已过期，可以重新提交上一条消息。';
  }
  return '任务可能仍在后台运行。重新连接会从已保存的进度继续，不会重复创建任务。';
}

export { TaskFailureNotice, friendlyError };
