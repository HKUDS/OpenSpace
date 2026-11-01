---
name: enhance-typescript-ui-components
description: Add visual features (thumbnails, badges, dynamic styling) to TypeScript components by coordinating TypeScript logic changes with CSS updates
---

# Enhance TypeScript UI Components

A systematic workflow for adding visual enhancements to TypeScript UI components, including thumbnails, color-coded badges, and dynamic styling. This pattern applies to component files that render data items (lists, panels, cards, etc.).

## When to Use This Skill

- Adding visual indicators (badges, labels, icons) to existing components
- Implementing thumbnail images with fallback placeholders
- Applying dynamic, data-driven colors or styles
- Coordinating TypeScript component logic with CSS styling

## Workflow Steps

### 1. Add Constants at File Top

Define color mappings, size constants, and configuration data near the top of the TypeScript file, after imports but before the class definition.

```typescript
// Color mappings for categories/sources
const SOURCE_COLORS: { [key: string]: string } = {
  'Category A': '#FF6B6B',
  'Category B': '#4ECDC4',
  'Category C': '#45B7D1',
  'default': '#95A5A6'
};

// Size constants
const THUMBNAIL_SIZE = 48; // pixels
const BADGE_HEIGHT = 20;
```

**Why**: Centralizes configuration for easy maintenance and avoids magic values scattered throughout code.

### 2. Implement Helper Methods

Create private helper methods for dynamic style calculations and data transformations. Place these before the main render method.

```typescript
private getColorForItem(item: DataItem): string {
  return SOURCE_COLORS[item.category] || SOURCE_COLORS['default'];
}

private sortItemsByDate(items: DataItem[]): DataItem[] {
  return [...items].sort((a, b) => 
    new Date(b.date).getTime() - new Date(a.date).getTime()
  );
}

private getThumbnailUrl(item: DataItem): string {
  return item.imageUrl || `https://via.placeholder.com/${THUMBNAIL_SIZE}`;
}
```

**Why**: Separates logic from presentation, makes code testable, and improves readability.

### 3. Sort Data Before Rendering

If displaying lists, apply sorting transformations before building HTML.

```typescript
public render(): void {
  const sortedItems = this.sortItemsByDate(this.items);
  const html = sortedItems.map(item => this.renderItem(item)).join('');
  this.container.innerHTML = html;
}
```

**Why**: Ensures consistent presentation order and separates data transformation from rendering.

### 4. Build HTML with Template Literals

Use template literals for HTML construction with conditional rendering for optional fields.

```typescript
private renderItem(item: DataItem): string {
  const itemColor = this.getColorForItem(item);
  const thumbnailUrl = this.getThumbnailUrl(item);
  
  return `
    <div class="item-card">
      ${item.imageUrl ? `
        <img 
          src="${thumbnailUrl}" 
          alt="${item.title}"
          class="item-thumbnail"
        />
      ` : `
        <div class="item-thumbnail-placeholder"></div>
      `}
      
      <div class="item-content">
        <h3>${item.title}</h3>
        ${item.category ? `
          <span 
            class="item-badge" 
            style="background-color: ${itemColor};"
          >
            ${item.category}
          </span>
        ` : ''}
        <p>${item.description}</p>
      </div>
    </div>
  `;
}
```

**Why**: Enables conditional rendering and keeps HTML structure clear. Dynamic values can be safely interpolated.

### 5. Apply Inline Styles for Dynamic Values Only

Use inline `style` attributes **only** for data-driven dynamic values (colors, sizes from data). Keep structural/layout styles in CSS.

```typescript
// ✅ Good - dynamic color from data
style="background-color: ${this.getColorForItem(item)};"

// ❌ Bad - structural styling should be in CSS
style="display: flex; padding: 10px; background-color: ${color};"

// ✅ Better - split concerns
class="item-badge" style="background-color: ${color};"
```

**Why**: Maintains separation of concerns—data-driven values in TS, structural styles in CSS.

### 6. Create or Update Corresponding CSS

For each new HTML element or class added in TypeScript, add corresponding CSS rules. Focus on:
- Layout and positioning
- Static colors and backgrounds
- Sizing (when not dynamic)
- Spacing (margin, padding)
- Typography

```css
/* Component container */
.item-card {
  display: flex;
  gap: 12px;
  padding: 16px;
  border-bottom: 1px solid #eee;
}

/* Thumbnail styles */
.item-thumbnail {
  width: 48px;
  height: 48px;
  border-radius: 6px;
  object-fit: cover;
  flex-shrink: 0;
}

.item-thumbnail-placeholder {
  width: 48px;
  height: 48px;
  border-radius: 6px;
  background-color: #e0e0e0;
  flex-shrink: 0;
}

/* Badge styles (color applied inline) */
.item-badge {
  display: inline-block;
  padding: 4px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  color: white;
  text-transform: uppercase;
}
```

**Why**: CSS handles layout and static styling efficiently; inline styles only for truly dynamic values.

### 7. Coordinate Changes Across Files

**Checklist for each enhancement**:
- [ ] TypeScript: Add constants/configuration
- [ ] TypeScript: Implement helper methods
- [ ] TypeScript: Update HTML template
- [ ] TypeScript: Add inline styles for dynamic values
- [ ] CSS: Add structural styles for new elements
- [ ] CSS: Ensure responsive behavior if needed
- [ ] Test with real data and edge cases (missing images, long text, etc.)

## Best Practices

1. **Fallback handling**: Always provide fallbacks for optional data (placeholder images, default colors, etc.)
2. **Escape user data**: If rendering user-provided content, sanitize/escape to prevent XSS
3. **Keep methods focused**: Each helper method should do one thing well
4. **Use semantic class names**: Prefer `.item-thumbnail` over `.img-48`
5. **Test edge cases**: Empty lists, missing fields, very long text, special characters

## Example: Adding Source Badges

**Before** (simple list):
```typescript
items.forEach(item => {
  html += `<div>${item.title}</div>`;
});
```

**After** (enhanced with badges):
```typescript
// 1. Add constants
const SOURCE_COLORS = {
  'TechNews': '#3498db',
  'default': '#95a5a6'
};

// 2. Helper method
private getSourceColor(source: string): string {
  return SOURCE_COLORS[source] || SOURCE_COLORS['default'];
}

// 3. Enhanced template
items.forEach(item => {
  const color = this.getSourceColor(item.source);
  html += `
    <div class="news-item">
      <span class="source-badge" style="background-color: ${color};">
        ${item.source}
      </span>
      <div>${item.title}</div>
    </div>
  `;
});

// 4. CSS (in stylesheet)
// .source-badge {
//   padding: 2px 6px;
//   border-radius: 3px;
//   font-size: 11px;
//   color: white;
// }
```

## Common Pitfalls

- **Mixing concerns**: Don't put all layout styles inline; use CSS for structure
- **Forgetting fallbacks**: Always handle missing/null optional fields
- **Hard-coded values**: Extract magic numbers and colors to constants
- **Skipping CSS updates**: Every new HTML structure needs corresponding CSS
- **Not testing edge cases**: Test with missing images, empty strings, special characters