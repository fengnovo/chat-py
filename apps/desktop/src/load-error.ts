export interface LoadErrorGate {
  tryStart(): boolean;
  finish(): void;
}

export function createLoadErrorGate(): LoadErrorGate {
  let active = false;

  return {
    tryStart() {
      if (active) {
        return false;
      }
      active = true;
      return true;
    },
    finish() {
      active = false;
    },
  };
}
