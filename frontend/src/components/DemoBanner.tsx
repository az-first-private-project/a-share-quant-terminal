/**
 * 只读演示模式横幅（V9 P0-1）
 * 始终显示在页面最顶部，提醒访问者数据来自静态快照，不连接后端。
 */
export default function DemoBanner() {
  return (
    <div
      role="status"
      className="relative z-50 border-b border-brass/30 bg-brass/[0.08] px-4 py-1.5 text-center text-[11px] font-medium tracking-[0.04em] text-brass-strong dark:text-brass"
    >
      演示数据 · 只读 · 不连接后端
      <span className="ml-2 text-muted-foreground">
        （快照由 pipeline demo 生成，可能停留在构建日）
      </span>
    </div>
  )
}
