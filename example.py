import asyncio
import time
from agentview import tracker


# ── async decorator ───────────────────────────────────────────────────────

@tracker.step("搜尋資料")
async def search():
    await asyncio.sleep(1.0)
    return ["article1", "article2", "article3"]


# ── sync decorator ────────────────────────────────────────────────────────

@tracker.step("儲存結果")
def save(data):
    time.sleep(0.4)


# ── main ─────────────────────────────────────────────────────────────────

async def main():
    with tracker.session():
        articles = await search()

        # message：顯示目前處理進度說明
        async with tracker.step("分析內容") as step:
            for i, article in enumerate(articles, 1):
                step.set_message(f"正在分析 {article}")
                await asyncio.sleep(0.4)
            step.set_message(None)

        # progress：顯示進度條
        with tracker.step("向量化") as step:
            total = 10
            for i in range(total):
                step.set_progress(i + 1, total)
                time.sleep(0.1)

        # sub-steps + message 組合
        with tracker.step("生成報告") as step:
            with step.sub_step("撰寫摘要") as sub:
                sub.set_message("處理關鍵詞...")
                time.sleep(0.5)
            with step.sub_step("輸出檔案"):
                time.sleep(0.3)

        save(articles)

    print("完成！")


if __name__ == "__main__":
    asyncio.run(main())
