'use client';

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

import CustomerServiceChat from '@/app/customer-service/customer-service-chat';
import { Icon } from './resilient-chat/icon';

interface AiServiceWidgetProps {
  open?: boolean;
  onOpen?: () => void;
  onClose?: () => void;
}

export function AiServiceWidget({ open, onOpen, onClose }: AiServiceWidgetProps) {
  const isControlled = open !== undefined;
  const isOpen = isControlled ? open : false;
  const [internalOpen, setInternalOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const effectiveOpen = isControlled ? isOpen : internalOpen;

  const handleOpen = () => {
    if (isControlled) {
      onOpen?.();
    } else {
      setInternalOpen(true);
    }
  };

  const handleClose = () => {
    if (isControlled) {
      onClose?.();
    } else {
      setInternalOpen(false);
    }
  };

  // 点击面板外部关闭
  useEffect(() => {
    if (!effectiveOpen) return;

    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (
        panelRef.current &&
        !panelRef.current.contains(target) &&
        triggerRef.current &&
        !triggerRef.current.contains(target)
      ) {
        handleClose();
      }
    };

    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [effectiveOpen]);

  if (isControlled && !effectiveOpen) {
    return null;
  }

  return createPortal(
    <div className="ai-service-widget-root">
      {effectiveOpen ? (
        <div ref={panelRef} className="ai-service-panel" role="dialog" aria-label="AI 客服">
          <CustomerServiceChat className="cs-widget" onClose={handleClose} />
        </div>
      ) : (
        <button
          ref={triggerRef}
          type="button"
          className="ai-service-trigger"
          aria-label="打开 AI 客服"
          onClick={handleOpen}
        >
          <Icon name="support" size={22} />
        </button>
      )}
    </div>,
    document.body,
  );
}
