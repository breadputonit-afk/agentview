"""Demo script for agentview — runs a simulated AI research agent."""
import asyncio
import time
from agentview import tracker


@tracker.step("搜尋文獻")
async def search(query: str):
    await asyncio.sleep(0.8)
    return ["paper_a.pdf", "paper_b.pdf", "paper_c.pdf"]


@tracker.step("下載資料")
async def download(papers: list[str]):
    await asyncio.sleep(0.6)
    return papers


async def main():
    with tracker.session():

        papers = await search("transformer attention mechanism")

        await download(papers)

        async with tracker.step("分析內容") as step:
            for i, paper in enumerate(papers, 1):
                step.set_message(f"閱讀 {paper}")
                await asyncio.sleep(0.5)
            step.set_message(None)

        with tracker.step("向量化") as step:
            for i in range(10):
                step.set_progress(i + 1, 10)
                time.sleep(0.08)

        with tracker.step("生成摘要") as step:
            with step.sub_step("提取關鍵詞"):
                time.sleep(0.4)
            with step.sub_step("撰寫段落"):
                time.sleep(0.5)
            with step.sub_step("格式輸出"):
                time.sleep(0.3)

    print("\n✓ 分析完成")


if __name__ == "__main__":
    asyncio.run(main())
