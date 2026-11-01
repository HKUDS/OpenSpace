---
name: virtual-list
description: Implement efficient virtual scrolling for rendering large lists with DOM recycling, chunk-based rendering, and performance optimizations
---

# Virtual List Implementation Skill

## Overview

This skill teaches you how to implement a high-performance virtual scrolling list component that can efficiently render thousands or millions of items by only creating DOM nodes for visible items plus a small buffer.

## Analysis of Reference Implementation

The VirtualList component from `/worldmonitor/src/components/VirtualList.ts` demonstrates professional-grade virtual scrolling with the following patterns:

### 1. **Chunk-Based Rendering Pattern**

The implementation uses two complementary strategies:

#### VirtualList (Fixed-Height Items)
- **Item Pool Management**: Maintains a pool of reusable DOM elements (`PooledElement[]`)
- **Viewport Calculation**: Calculates visible range based on `scrollTop`, `itemHeight`, and viewport dimensions
- **Overscan Buffer**: Renders extra items above/below viewport (`overscan` parameter) to prevent flickering during scroll

#### WindowedList (Variable-Height Items)
- **Chunk-Based Approach**: Divides items into chunks (default 10 items per chunk)
- **Lazy Chunk Rendering**: Only renders chunks that are visible or within buffer range
- **Placeholder Elements**: Creates placeholders for all chunks upfront, renders content on-demand

### 2. **Scroll Listener Optimization**

```typescript
private handleScroll = (): void => {
  if (this.scrollRAF !== null) return;
  
  this.scrollRAF = requestAnimationFrame(() => {
    this.scrollRAF = null;
    if (!this.isDestroyed) {
      this.updateVisibleRange();
    }
  });
};
```

**Key Optimizations:**
- **RequestAnimationFrame Throttling**: Prevents multiple renders in same frame
- **Passive Listeners**: `{ passive: true }` improves scroll performance
- **Debounce Check**: Guards against duplicate RAF calls
- **Cleanup Check**: Prevents rendering after destruction

### 3. **DOM Recycling Strategy**

The implementation uses a sophisticated two-pass recycling algorithm:

**Pass 1: Identify Reusable Elements**
```typescript
for (const pooled of this.itemPool) {
  if (pooled.currentIndex >= visibleStart && pooled.currentIndex < visibleEnd) {
    usedIndices.add(pooled.currentIndex);
  }
}
```

**Pass 2: Recycle and Reassign**
```typescript
while (poolIndex < this.itemPool.length) {
  const pooled = this.itemPool[poolIndex]!;
  if (pooled.currentIndex < visibleStart || pooled.currentIndex >= visibleEnd) {
    // Recycle this element
    if (this.onRecycle) {
      this.onRecycle(pooled.element);
    }
    pooled.currentIndex = i;
    this.renderItem(i, pooled.element);
    pooled.element.style.transform = `translateY(${i * this.itemHeight}px)`;
    poolIndex++;
    break;
  }
  poolIndex++;
}
```

**Benefits:**
- Minimizes DOM manipulation
- Reuses existing elements when possible
- Allows cleanup of event listeners via `onRecycle` callback
- Uses `transform: translateY()` for GPU-accelerated positioning

### 4. **Performance Optimizations**

#### Spacer Elements for Virtual Height
```typescript
this.topSpacer.style.height = `${visibleStart * this.itemHeight}px`;
this.bottomSpacer.style.height = `${Math.max(0, (this.totalItems - visibleEnd) * this.itemHeight)}px`;
```

- Creates illusion of full list height without rendering all items
- Maintains accurate scrollbar size and position

#### Skip Unnecessary Updates
```typescript
if (visibleStart === this.visibleStart && visibleEnd === this.visibleEnd) {
  return;
}
```

#### CSS Positioning Strategy
```typescript
element.style.position = 'absolute';
element.style.top = '0';
element.style.left = '0';
element.style.right = '0';
element.style.transform = 'translateY(-9999px)'; // Hide off-screen
```

- Absolute positioning for precise control
- Transform for GPU acceleration
- Off-screen positioning instead of display:none (preserves element state)

#### ResizeObserver Integration
```typescript
if (typeof ResizeObserver !== 'undefined') {
  this.resizeObserver = new ResizeObserver(() => {
    if (!this.isDestroyed) {
      this.updateVisibleRange();
    }
  });
  this.resizeObserver.observe(this.viewport);
}
```

- Automatically recalculates visible range when viewport resizes
- Progressive enhancement (checks for browser support)

## Complete TypeScript Implementation Template

Below is a simplified but fully functional virtual list implementation that you can drop into any vanilla TypeScript project:

```typescript
/**
 * SimpleVirtualList - A simplified virtual scrolling implementation
 * 
 * Usage:
 * ```typescript
 * const virtualList = new SimpleVirtualList({
 *   container: document.getElementById('my-container')!,
 *   itemHeight: 50,
 *   totalItems: 100000,
 *   renderItem: (index, element) => {
 *     element.textContent = `Item ${index}`;
 *   }
 * });
 * ```
 */

export interface SimpleVirtualListOptions {
  /** Container element to render the virtual list into */
  container: HTMLElement;
  
  /** Fixed height of each item in pixels */
  itemHeight: number;
  
  /** Total number of items in the list */
  totalItems: number;
  
  /** Callback to render content for an item */
  renderItem: (index: number, element: HTMLElement) => void;
  
  /** Number of items to render beyond visible area (default: 5) */
  overscan?: number;
  
  /** Optional callback when item element is recycled */
  onRecycle?: (element: HTMLElement) => void;
}

interface VirtualItem {
  element: HTMLElement;
  index: number;
}

export class SimpleVirtualList {
  // Configuration
  private container: HTMLElement;
  private itemHeight: number;
  private totalItems: number;
  private overscan: number;
  private renderItem: (index: number, element: HTMLElement) => void;
  private onRecycle?: (element: HTMLElement) => void;

  // DOM Structure
  private viewport: HTMLElement;
  private contentWrapper: HTMLElement;
  private topSpacer: HTMLElement;
  private bottomSpacer: HTMLElement;

  // State
  private itemPool: VirtualItem[] = [];
  private visibleStart = 0;
  private visibleEnd = 0;
  private rafId: number | null = null;
  private destroyed = false;

  constructor(options: SimpleVirtualListOptions) {
    this.container = options.container;
    this.itemHeight = options.itemHeight;
    this.totalItems = options.totalItems;
    this.overscan = options.overscan ?? 5;
    this.renderItem = options.renderItem;
    this.onRecycle = options.onRecycle;

    this.setupDOM();
    this.attachEventListeners();
    this.updateVisibleRange();
  }

  /**
   * Set up the DOM structure for virtual scrolling
   */
  private setupDOM(): void {
    // Clear container
    this.container.innerHTML = '';

    // Create viewport (scrollable container)
    this.viewport = document.createElement('div');
    this.viewport.style.cssText = `
      width: 100%;
      height: 100%;
      overflow-y: auto;
      position: relative;
    `;

    // Create content wrapper
    this.contentWrapper = document.createElement('div');
    const totalHeight = this.totalItems * this.itemHeight;
    this.contentWrapper.style.cssText = `
      position: relative;
      width: 100%;
      height: ${totalHeight}px;
    `;

    // Create spacers
    this.topSpacer = document.createElement('div');
    this.topSpacer.style.cssText = `
      height: 0px;
      width: 100%;
    `;

    this.bottomSpacer = document.createElement('div');
    this.bottomSpacer.style.cssText = `
      height: ${totalHeight}px;
      width: 100%;
    `;

    // Assemble DOM
    this.contentWrapper.appendChild(this.topSpacer);
    this.contentWrapper.appendChild(this.bottomSpacer);
    this.viewport.appendChild(this.contentWrapper);
    this.container.appendChild(this.viewport);
  }

  /**
   * Attach scroll event listeners with optimization
   */
  private attachEventListeners(): void {
    this.viewport.addEventListener('scroll', this.handleScroll, { passive: true });
  }

  /**
   * Throttled scroll handler using requestAnimationFrame
   */
  private handleScroll = (): void => {
    // Prevent multiple RAF calls per frame
    if (this.rafId !== null) return;

    this.rafId = requestAnimationFrame(() => {
      this.rafId = null;
      
      if (!this.destroyed) {
        this.updateVisibleRange();
      }
    });
  };

  /**
   * Calculate and update which items should be visible
   */
  private updateVisibleRange(): void {
    const scrollTop = this.viewport.scrollTop;
    const viewportHeight = this.viewport.clientHeight;

    // Calculate visible indices
    const startIndex = Math.floor(scrollTop / this.itemHeight);
    const endIndex = Math.ceil((scrollTop + viewportHeight) / this.itemHeight);

    // Add overscan buffer
    const bufferedStart = Math.max(0, startIndex - this.overscan);
    const bufferedEnd = Math.min(this.totalItems, endIndex + this.overscan);

    // Skip if range hasn't changed
    if (bufferedStart === this.visibleStart && bufferedEnd === this.visibleEnd) {
      return;
    }

    this.visibleStart = bufferedStart;
    this.visibleEnd = bufferedEnd;

    // Update spacers to maintain scroll position
    this.topSpacer.style.height = `${bufferedStart * this.itemHeight}px`;
    const bottomHeight = Math.max(0, (this.totalItems - bufferedEnd) * this.itemHeight);
    this.bottomSpacer.style.height = `${bottomHeight}px`;

    // Render visible items
    this.renderVisibleItems();
  }

  /**
   * Render or update items in the visible range using DOM recycling
   */
  private renderVisibleItems(): void {
    const visibleCount = this.visibleEnd - this.visibleStart;

    // Ensure we have enough pooled elements
    this.ensurePoolSize(visibleCount);

    // Track which indices are currently in use
    const activeIndices = new Set<number>();

    // First pass: keep elements that are still visible
    for (const item of this.itemPool) {
      if (item.index >= this.visibleStart && item.index < this.visibleEnd) {
        activeIndices.add(item.index);
      }
    }

    // Second pass: recycle and assign new indices
    let poolIndex = 0;
    for (let i = this.visibleStart; i < this.visibleEnd; i++) {
      // Skip if already rendered
      if (activeIndices.has(i)) continue;

      // Find an element to recycle
      while (poolIndex < this.itemPool.length) {
        const item = this.itemPool[poolIndex];
        
        // Check if this element can be recycled
        if (item.index < this.visibleStart || item.index >= this.visibleEnd) {
          // Call recycle callback
          if (this.onRecycle) {
            this.onRecycle(item.element);
          }

          // Update item
          item.index = i;
          this.renderItem(i, item.element);
          this.positionItem(item);
          
          poolIndex++;
          break;
        }
        
        poolIndex++;
      }
    }

    // Third pass: update positions and visibility
    for (const item of this.itemPool) {
      if (item.index >= this.visibleStart && item.index < this.visibleEnd) {
        // Ensure proper position
        this.positionItem(item);
        item.element.style.visibility = 'visible';
      } else {
        // Hide off-screen items
        item.element.style.visibility = 'hidden';
        item.element.style.transform = 'translateY(-9999px)';
      }
    }
  }

  /**
   * Position an item element using transform for GPU acceleration
   */
  private positionItem(item: VirtualItem): void {
    const yOffset = item.index * this.itemHeight;
    item.element.style.transform = `translateY(${yOffset}px)`;
  }

  /**
   * Ensure the pool has enough elements
   */
  private ensurePoolSize(requiredSize: number): void {
    while (this.itemPool.length < requiredSize) {
      const element = this.createItemElement();
      const item: VirtualItem = {
        element,
        index: -1,
      };
      
      this.itemPool.push(item);
      
      // Insert before bottom spacer
      this.contentWrapper.insertBefore(element, this.bottomSpacer);
    }
  }

  /**
   * Create a new item element with proper styling
   */
  private createItemElement(): HTMLElement {
    const element = document.createElement('div');
    element.style.cssText = `
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: ${this.itemHeight}px;
      box-sizing: border-box;
      visibility: hidden;
      transform: translateY(-9999px);
    `;
    element.classList.add('virtual-list-item');
    return element;
  }

  /**
   * Update the total number of items
   */
  setTotalItems(count: number): void {
    this.totalItems = count;
    
    // Update content height
    const totalHeight = this.totalItems * this.itemHeight;
    this.contentWrapper.style.height = `${totalHeight}px`;
    
    // Recalculate visible range
    this.updateVisibleRange();
  }

  /**
   * Scroll to a specific item index
   */
  scrollToIndex(index: number, smooth = false): void {
    const offset = Math.max(0, Math.min(index, this.totalItems - 1)) * this.itemHeight;
    this.viewport.scrollTo({
      top: offset,
      behavior: smooth ? 'smooth' : 'auto',
    });
  }

  /**
   * Force refresh all visible items
   */
  refresh(): void {
    // Mark all items as needing re-render
    for (const item of this.itemPool) {
      item.index = -1;
    }
    
    // Trigger update
    this.updateVisibleRange();
  }

  /**
   * Get the current scroll position info
   */
  getScrollInfo(): { scrollTop: number; visibleStart: number; visibleEnd: number } {
    return {
      scrollTop: this.viewport.scrollTop,
      visibleStart: this.visibleStart,
      visibleEnd: this.visibleEnd,
    };
  }

  /**
   * Clean up resources and remove event listeners
   */
  destroy(): void {
    this.destroyed = true;

    // Cancel pending animation frame
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId);
      this.rafId = null;
    }

    // Remove event listeners
    this.viewport.removeEventListener('scroll', this.handleScroll);

    // Clear pool
    this.itemPool = [];

    // Clear container
    this.container.innerHTML = '';
  }
}

// ============================================================================
// CSS Styles (add to your stylesheet or inject programmatically)
// ============================================================================

/**
 * Recommended CSS for virtual list container:
 * 
 * ```css
 * .virtual-list-container {
 *   width: 100%;
 *   height: 400px; // or whatever height you need
 *   border: 1px solid #ddd;
 *   overflow: hidden;
 * }
 * 
 * .virtual-list-item {
 *   border-bottom: 1px solid #eee;
 *   padding: 10px;
 *   display: flex;
 *   align-items: center;
 * }
 * 
 * .virtual-list-item:hover {
 *   background-color: #f5f5f5;
 * }
 * ```
 */

// ============================================================================
// Usage Example
// ============================================================================

/**
 * Example 1: Basic Usage
 */
export function exampleBasicUsage(): void {
  const container = document.createElement('div');
  container.className = 'virtual-list-container';
  container.style.cssText = 'width: 500px; height: 400px; border: 1px solid #ddd;';
  document.body.appendChild(container);

  const virtualList = new SimpleVirtualList({
    container,
    itemHeight: 50,
    totalItems: 100000,
    renderItem: (index, element) => {
      element.innerHTML = `
        <div style="padding: 10px; border-bottom: 1px solid #eee;">
          <strong>Item ${index}</strong>
          <span style="margin-left: 20px; color: #666;">
            ${new Date(Date.now() - index * 1000 * 60).toLocaleString()}
          </span>
        </div>
      `;
    },
  });

  // Scroll to item 5000 after 2 seconds
  setTimeout(() => {
    virtualList.scrollToIndex(5000, true);
  }, 2000);
}

/**
 * Example 2: With Cleanup and Event Listeners
 */
export function exampleWithCleanup(): void {
  const container = document.getElementById('list-container') as HTMLElement;

  const virtualList = new SimpleVirtualList({
    container,
    itemHeight: 60,
    totalItems: 50000,
    renderItem: (index, element) => {
      element.innerHTML = `
        <div class="item-content">
          <h4>User ${index}</h4>
          <button class="delete-btn" data-index="${index}">Delete</button>
        </div>
      `;

      // Add event listener
      const btn = element.querySelector('.delete-btn') as HTMLElement;
      if (btn) {
        btn.addEventListener('click', () => {
          console.log(`Delete item ${index}`);
        });
      }
    },
    onRecycle: (element) => {
      // Clean up event listeners when element is recycled
      const btn = element.querySelector('.delete-btn') as HTMLElement;
      if (btn) {
        // Clone and replace to remove all listeners
        const newBtn = btn.cloneNode(true);
        btn.parentNode?.replaceChild(newBtn, btn);
      }
    },
  });

  // Example: Update total items dynamically
  setTimeout(() => {
    virtualList.setTotalItems(100000);
  }, 5000);
}

/**
 * Example 3: Integration with Data Source
 */
export class DataVirtualList<T> {
  private virtualList: SimpleVirtualList;
  private data: T[] = [];
  private renderer: (item: T, index: number, element: HTMLElement) => void;

  constructor(
    container: HTMLElement,
    itemHeight: number,
    renderer: (item: T, index: number, element: HTMLElement) => void
  ) {
    this.renderer = renderer;

    this.virtualList = new SimpleVirtualList({
      container,
      itemHeight,
      totalItems: 0,
      renderItem: (index, element) => {
        if (index < this.data.length) {
          this.renderer(this.data[index], index, element);
        }
      },
    });
  }

  setData(data: T[]): void {
    this.data = data;
    this.virtualList.setTotalItems(data.length);
  }

  refresh(): void {
    this.virtualList.refresh();
  }

  scrollToIndex(index: number): void {
    this.virtualList.scrollToIndex(index, true);
  }

  destroy(): void {
    this.virtualList.destroy();
  }
}

// Example usage with typed data
interface User {
  id: number;
  name: string;
  email: string;
}

export function exampleTypedData(): void {
  const container = document.getElementById('user-list') as HTMLElement;

  const userList = new DataVirtualList<User>(
    container,
    80,
    (user, index, element) => {
      element.innerHTML = `
        <div style="padding: 15px; border-bottom: 1px solid #e0e0e0;">
          <div style="font-weight: bold; font-size: 16px;">${user.name}</div>
          <div style="color: #666; font-size: 14px;">${user.email}</div>
          <div style="color: #999; font-size: 12px;">ID: ${user.id}</div>
        </div>
      `;
    }
  );

  // Generate sample data
  const users: User[] = Array.from({ length: 100000 }, (_, i) => ({
    id: i,
    name: `User ${i}`,
    email: `user${i}@example.com`,
  }));

  userList.setData(users);
}
```

## Key Implementation Patterns

### 1. **DOM Structure**
```
Container
└── Viewport (scrollable)
    └── ContentWrapper (full height)
        ├── TopSpacer (variable height)
        ├── Item Elements (absolute positioned)
        └── BottomSpacer (variable height)
```

### 2. **Scroll Event Flow**
```
Scroll Event → RAF Throttle → Calculate Visible Range → Update Spacers → Recycle Items → Render Content
```

### 3. **Element Recycling Algorithm**
1. Calculate new visible range
2. Identify elements still in range (reuse)
3. Find elements out of range (recycle candidates)
4. Assign recycled elements to new indices
5. Update positions with transform
6. Hide off-screen elements

### 4. **Performance Checklist**
- ✅ Use `requestAnimationFrame` for scroll throttling
- ✅ Add `{ passive: true }` to scroll listeners
- ✅ Use `transform` instead of `top/left` for positioning
- ✅ Implement overscan buffer to prevent flickering
- ✅ Skip updates when visible range hasn't changed
- ✅ Use absolute positioning for precise control
- ✅ Recycle DOM elements instead of creating/destroying
- ✅ Provide cleanup callback for event listeners

## When to Use Virtual Lists

### ✅ Use When:
- Rendering 1,000+ items
- Items have uniform/predictable height
- Scrolling performance is critical
- Memory constraints are a concern
- Data is paginated or infinite

### ❌ Avoid When:
- Lists are small (<100 items)
- Items have highly variable heights
- Complex nested scrolling is required
- You need to support keyboard navigation to all items
- CSS grid/flexbox layouts are essential

## Common Pitfalls

1. **Forgetting to clean up event listeners** → Memory leaks
   - Solution: Use `onRecycle` callback

2. **Not using passive listeners** → Janky scrolling
   - Solution: `{ passive: true }`

3. **Synchronous expensive rendering** → Dropped frames
   - Solution: Keep `renderItem` fast, defer heavy work

4. **Variable item heights without measurement** → Misaligned items
   - Solution: Use fixed heights or implement height caching

5. **Not handling resize events** → Broken layout
   - Solution: Add ResizeObserver or window resize listener

## Advanced Enhancements

### Dynamic Height Support
```typescript
// Cache measured heights
private heightCache = new Map<number, number>();

private measureItem(index: number, element: HTMLElement): number {
  if (!this.heightCache.has(index)) {
    const height = element.getBoundingClientRect().height;
    this.heightCache.set(index, height);
  }
  return this.heightCache.get(index)!;
}
```

### Sticky Headers
```typescript
// Track section headers
private sectionHeaders = new Map<number, string>();

private updateStickyHeader(): void {
  const scrollTop = this.viewport.scrollTop;
  const currentSection = this.getSectionAtOffset(scrollTop);
  this.stickyHeaderElement.textContent = currentSection;
}
```

### Bidirectional Scrolling
```typescript
// Support horizontal + vertical scrolling
private updateVisibleRange2D(): void {
  const scrollX = this.viewport.scrollLeft;
  const scrollY = this.viewport.scrollTop;
  
  const colStart = Math.floor(scrollX / this.itemWidth);
  const rowStart = Math.floor(scrollY / this.itemHeight);
  // ... calculate visible grid cells
}
```

## Testing Strategies

```typescript
// Test with various scenarios
describe('SimpleVirtualList', () => {
  it('renders only visible items', () => {
    const list = new SimpleVirtualList({...});
    expect(list.getScrollInfo().visibleEnd - list.getScrollInfo().visibleStart)
      .toBeLessThan(50); // Even with 100k items
  });

  it('recycles elements on scroll', () => {
    const recycledIndices: number[] = [];
    const list = new SimpleVirtualList({
      onRecycle: (el) => recycledIndices.push(parseInt(el.dataset.index!)),
      ...
    });
    
    list.scrollToIndex(1000);
    expect(recycledIndices.length).toBeGreaterThan(0);
  });

  it('handles rapid scrolling', async () => {
    const list = new SimpleVirtualList({...});
    for (let i = 0; i < 100; i++) {
      list.scrollToIndex(i * 100);
      await nextFrame();
    }
    // Should not crash or have visual glitches
  });
});
```

## Summary

Virtual scrolling is a critical technique for rendering large datasets efficiently. The key principles are:

1. **Only render what's visible** - Create DOM nodes for viewport + buffer only
2. **Recycle aggressively** - Reuse existing elements instead of creating new ones
3. **Use spacers for height** - Maintain scroll position without rendering all items
4. **Optimize scroll handling** - Use RAF throttling and passive listeners
5. **Position with transforms** - Leverage GPU acceleration
6. **Clean up properly** - Prevent memory leaks with recycle callbacks

This implementation can handle millions of items with smooth 60fps scrolling and minimal memory usage.
