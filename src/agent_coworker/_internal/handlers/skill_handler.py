"""AgentFax Skill Handler — Skill Card discovery."""

import logging

logger = logging.getLogger("agentfax.handlers.skill")


def register_skill_handlers(router, executor, data_dir: str, peer_skill_cache=None):
    """Register skill-related handlers with the router."""

    @router.handler("skill_card_query")
    def handle_skill_card_query(msg, ctx):
        payload = msg.get("payload", {})
        tags_filter = payload.get("tags")
        names_filter = payload.get("names")
        all_skills = executor.list_skills()
        cards = []
        for card in all_skills:
            if names_filter and card.get("name") not in names_filter:
                continue
            if tags_filter:
                card_tags = card.get("tags", [])
                if not any(t in card_tags for t in tags_filter):
                    continue
            cards.append(card)
        return {"type": "skill_card_list", "payload": {"skills": cards, "count": len(cards)}}

    @router.handler("skill_card_list")
    def handle_skill_card_list(msg, ctx):
        payload = msg.get("payload", {})
        sender = msg.get("sender_id", "unknown")
        skills = payload.get("skills", [])
        if peer_skill_cache and skills:
            peer_skill_cache.store_cards(sender, skills)
        if ctx.peer_manager:
            ctx.peer_manager.update_capabilities(sender, capabilities={"skills": skills})
        return None

    @router.handler("skill_card_get")
    def handle_skill_card_get(msg, ctx):
        payload = msg.get("payload", {})
        skill_name = payload.get("skill_name")
        skill_def = executor.get_skill(skill_name) if skill_name else None
        if not skill_def:
            return {"type": "task_error", "payload": {
                "error_code": "SKILL_NOT_FOUND",
                "error_message": f"No skill named '{skill_name}'",
                "retryable": False, "scope": "routing",
            }}
        return {"type": "skill_card", "payload": {"card": skill_def.to_dict()}}

    @router.handler("skill_card")
    def handle_skill_card(msg, ctx):
        payload = msg.get("payload", {})
        sender = msg.get("sender_id", "unknown")
        card = payload.get("card", {})
        if peer_skill_cache and card:
            peer_skill_cache.store_cards(sender, [card])
        return None

    @router.handler("skill_query")
    def handle_skill_query(msg, ctx):
        cards = executor.list_skills()
        return {"type": "skill_list", "payload": {"skills": cards, "count": len(cards)}}

    @router.handler("skill_list")
    def handle_skill_list(msg, ctx):
        return handle_skill_card_list(msg, ctx)

    @router.handler("skill_install")
    def handle_skill_install(msg, ctx):
        return {"type": "skill_install_result", "payload": {
            "name": msg.get("payload", {}).get("name", "?"),
            "success": False, "error_code": "CODE_TRANSFER_FORBIDDEN",
            "error": "Remote code installation is not supported.",
        }}

    @router.handler("skill_install_result")
    def handle_skill_install_result(msg, ctx):
        return None
