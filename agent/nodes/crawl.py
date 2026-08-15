"""
爬取任务节点
- create_task_node:   根据意图创建爬取任务
- execute_crawl_node: 调用 tools.zsxq 执行爬取
"""
from datetime import datetime

from agent.state import AgentState, CrawlTask
from tools.zsxq.crawler import ZSXQCrawler


def create_task_node(state: AgentState) -> AgentState:
    """根据解析出的意图创建爬取任务"""
    if not state.parsed_intent:
        state.response = "无法解析您的指令"
        return state

    intent = state.parsed_intent
    action = intent.get("action")
    group_id = intent.get("group_id")

    if action == "unknown":
        state.response = (
            "支持的指令：\n"
            "- \"爬取最新10个话题 [群组ID]\"\n"
            "- \"爬取历史数据5页 [群组ID]\"\n"
            "- \"增量更新 [群组ID]\"\n"
            "- \"下载文件 [群组ID]\"\n"
            "- \"查看统计 [群组ID]\""
        )
        return state

    if not group_id:
        state.response = "请提供群组ID（6位以上数字）"
        return state

    task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    task = CrawlTask(
        task_id=task_id,
        task_type=action,
        group_id=group_id,
        params=intent.get("params", {}),
    )

    state.tasks.append(task)
    state.current_task = task
    state.log(f"创建任务: {task_id} ({action})")

    return state


def execute_crawl_node(state: AgentState) -> AgentState:
    """执行爬取任务"""
    task = state.current_task
    if not task:
        state.response = "无任务可执行"
        return state

    task.status = "running"
    task.started_at = datetime.now()
    state.log(f"开始执行任务: {task.task_id}")

    cookie = state.get_active_cookie()
    if not cookie:
        task.status = "failed"
        task.error = "无有效Cookie"
        state.response = "执行失败：无有效Cookie"
        return state

    try:
        crawler = ZSXQCrawler(cookie, task.group_id)

        if task.task_type == "latest":
            result = crawler.crawl_latest(task.params.get("count", 20))
        elif task.task_type == "historical":
            result = crawler.crawl_historical(pages=task.params.get("count", 10))
        elif task.task_type == "incremental":
            result = crawler.crawl_incremental(pages=task.params.get("count", 10))
        elif task.task_type == "update":
            result = crawler.crawl_latest_until_complete()
        else:
            result = {"error": "未知任务类型"}

        task.result = result
        task.status = "completed"
        task.completed_at = datetime.now()

        state.log(f"任务完成: {result}")
        state.response = (
            f"✅ 任务完成\n"
            f"类型: {task.task_type}\n"
            f"群组: {task.group_id}\n"
            f"结果: {result}"
        )

        crawler.close()

    except Exception as e:
        task.status = "failed"
        task.error = str(e)
        state.log(f"任务失败: {e}")
        state.response = f"❌ 执行失败: {e}"

    return state
