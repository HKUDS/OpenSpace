import React from "react";
import { Box, Text } from "ink";
import type { Notification } from "../context/notifications.js";
import { getColor } from "./design-system/theme.js";

type NotificationBannerProps = {
  notification: Notification | null;
};

function resolveNotificationColor(notification: Notification): string {
  if ("color" in notification && notification.color) {
    return notification.color;
  }

  switch (notification.priority) {
    case "immediate":
    case "high":
      return getColor("error");
    case "medium":
      return getColor("warning");
    case "low":
    default:
      return getColor("primary");
  }
}

function resolveNotificationText(notification: Notification): string | null {
  if ("text" in notification) {
    return notification.text;
  }
  return null;
}

export function NotificationBanner({
  notification,
}: NotificationBannerProps): React.ReactElement | null {
  if (!notification) return null;

  const text = resolveNotificationText(notification);
  if (!text) return null;

  const color = resolveNotificationColor(notification);

  return (
    <Box borderStyle="round" borderColor={color} paddingX={1}>
      <Text color={color}>{text}</Text>
    </Box>
  );
}
