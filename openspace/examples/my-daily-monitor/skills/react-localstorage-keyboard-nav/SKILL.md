---
name: react-localstorage-keyboard-nav
description: Create React components with localStorage persistence and keyboard navigation support
---

# React Components with localStorage Persistence & Keyboard Navigation

This skill guides you through creating reusable React components that:
1. Persist state to localStorage
2. Implement keyboard navigation
3. Follow modal/command palette patterns

## Core Pattern

```typescript
// 1. Define localStorage hooks
const useLocalStorage = (key: string, initialValue: any) => {
  const [value, setValue] = useState(() => {
    const stored = localStorage.getItem(key);
    return stored ? JSON.parse(stored) : initialValue;
  });

  useEffect(() => {
    localStorage.setItem(key, JSON.stringify(value));
  }, [key, value]);

  return [value, setValue];
};

// 2. Implement keyboard navigation
const useKeyboardNav = (items: any[], onSelect: (item: any) => void) => {
  const [selectedIndex, setSelectedIndex] = useState(0);

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      setSelectedIndex(prev => Math.min(prev + 1, items.length - 1));
    } else if (e.key === 'ArrowUp') {
      setSelectedIndex(prev => Math.max(prev - 1, 0));
    } else if (e.key === 'Enter') {
      onSelect(items[selectedIndex]);
    }
  };

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedIndex, items]);

  return { selectedIndex };
};
```

## Implementation Steps

1. **Create Component Scaffolding**
   ```bash
   mkdir -p src/components/{Modal,CommandPalette}
   touch src/components/Modal/SettingsModal.tsx
   touch src/components/CommandPalette/CommandPalette.tsx
   ```

2. **Implement localStorage Persistence**
   ```typescript
   // SettingsModal.tsx
   const SettingsModal = () => {
     const [settings, setSettings] = useLocalStorage('app-settings', DEFAULT_SETTINGS);
     // ... modal implementation
   };
   ```

3. **Add Keyboard Navigation**
   ```typescript
   // CommandPalette.tsx
   const CommandPalette = ({ commands }) => {
     const { selectedIndex } = useKeyboardNav(commands, (cmd) => cmd.action());
     // ... render commands with highlighted selectedIndex
   };
   ```

4. **Export Components**
   ```typescript
   // src/components/index.ts
   export * from './Modal/SettingsModal';
   export * from './CommandPalette/CommandPalette';
   ```

## Best Practices

- Create a `settings-keys.ts` file to manage localStorage keys
- Use TypeScript interfaces for settings/commands
- Implement proper focus management for accessibility
