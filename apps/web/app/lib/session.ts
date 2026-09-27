import {
  Conversation,
  appendAssistantMessage,
  appendUserMessage,
  createConversationHistory,
} from 'conversationalist';

export class ResilientSession {
  private conversation = new Conversation(
    createConversationHistory({ title: 'Resilient Chat Session' }),
  );

  private hasProvisionalAssistant = false;

  addUserMessage(content: string) {
    this.conversation.push(
      appendUserMessage(this.conversation.current, content),
    );
  }

  stageAssistant(content: string) {
    if (!content || this.hasProvisionalAssistant) return;
    this.conversation.push(
      appendAssistantMessage(this.conversation.current, content),
    );
    this.hasProvisionalAssistant = true;
  }

  commitAssistant(content: string) {
    if (!content) return;
    if (this.hasProvisionalAssistant) {
      this.conversation.undo();
    }
    this.conversation.push(
      appendAssistantMessage(this.conversation.current, content),
    );
    this.hasProvisionalAssistant = false;
  }

  rollbackAssistant() {
    if (!this.hasProvisionalAssistant || !this.conversation.canUndo) {
      return false;
    }
    this.conversation.undo();
    this.hasProvisionalAssistant = false;
    return true;
  }

  get stats() {
    return {
      revision: this.conversation.revision,
      messages: this.conversation.current.ids.length,
      canUndo: this.conversation.canUndo,
    };
  }
}
