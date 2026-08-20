#!/usr/bin/env python3
"""
State Manager — 状态文件管理
角色登记 / 子结构进度更新 / 章节完成 / 时间线记录

update-sub 命令特点（即时标记）：
  - 每次调用立即更新 novel_state.json 中的子结构状态
  - 不接受批量/延迟模式调用
  - 记录实际字数和完成时间
"""
import json, sys, hashlib
from pathlib import Path
from datetime import datetime

# 路径集中管理
from _path_utils import DATA_DIR
from nover_config import LENGTH_LABELS, LENGTH_CHAPTERS

# ── 核心规划字段保护 ──
# 这些字段一旦写入即不可修改（仅 status/word_count 等运行字段可变更）
IMMUTABLE_SCOPE = {
    "chapters": {"id", "title", "overview"},
    "sub_structures": {"title", "summary", "tone"},
    "characters": {"name", "role", "traits", "mbti", "archetype", "function", "aliases"},
    "top_level": {"project", "novel_info", "writing_style", "setting", "technical_notes"},
}

def _fingerprint(data: dict) -> str:
    """提取规划字段的指纹（剔除动态字段）。

    注意：所有 set 迭代必须 sorted——set 迭代顺序受 PYTHONHASHSEED 随机化影响，
    跨进程（plan-chapter / update-sub 各自独立子进程）顺序不同会导致同一内容的
    指纹不同，误触发"核心字段被非法修改"阻断。
    """
    parts = []
    # 顶层不可变
    for key in sorted(IMMUTABLE_SCOPE.get("top_level", set())):
        val = data.get(key)
        if val is not None:
            parts.append(json.dumps({key: val}, sort_keys=True, ensure_ascii=False))
    # 章节
    for ch in data.get("chapters", []):
        ch_id = ch.get("id", "")
        for f in sorted(IMMUTABLE_SCOPE["chapters"]):
            parts.append(f"{ch_id}.{f}={ch.get(f,'')}")
        # 子结构
        for sk, sv in sorted(ch.get("sub_structures", {}).items()):
            for f in sorted(IMMUTABLE_SCOPE["sub_structures"]):
                parts.append(f"{ch_id}.{sk}.{f}={sv.get(f,'')}")
    # 角色
    for c in data.get("characters", []):
        for f in sorted(IMMUTABLE_SCOPE["characters"]):
            parts.append(f"char.{c.get('name','')}.{f}={json.dumps(c.get(f,''), ensure_ascii=False)}")
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def _fingerprint_path(path: str) -> Path:
    p = Path(path)
    return p.parent / ".state_fingerprint.txt"


def load_state(path):
    """读取 novel state。文件缺失 → 自动尝试从备份恢复（data/novel/backups/）。

    这是所有读 state 的统一入口（plan_chapter_subs / replan / context_loader / novel_writer 全走这里）——
    之前 restore 只接在 novel_writer 开头，后续读 state 的地方文件缺失会裸抛 FileNotFoundError，
    用户看到"有备份还报错"。现在任何读点缺失都先尝试备份恢复，恢复成功返回数据，
    恢复失败抛明确中文错误（含备份路径提示），不静默重建。
    """
    p = Path(path)
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8-sig"))
    # 文件缺失 → 尝试备份恢复
    restored = _restore_from_backup(path)
    if restored is not None:
        return restored
    raise FileNotFoundError(
        f"小说项目状态缺失且无可用备份: {path}\n"
        f"（已检查 {Path(path).parent.parent.parent / 'backups' / Path(path).parent.parent.name}）\n"
        f"请重新生成大纲（新开会话），或从「输出」列表查看已完成的章节。"
    )


def _restore_from_backup(path):
    """从 data/novel/backups/<项目名>/ 完整恢复（state + chapters 正文），成功返回数据，失败返回 None。"""
    try:
        from ._path_utils import DATA_DIR
        import shutil as _sh
        p = Path(path)
        project_name = p.parent.parent.name
        if not project_name or project_name == "projects":
            return None
        backup_dir = DATA_DIR.parent / "backups" / project_name   # data/novel/backups/<name>/
        bak = backup_dir / "novel_state.json"
        if not bak.is_file():
            return None
        data = json.loads(bak.read_text(encoding="utf-8-sig"))
        # 恢复 state 文件本身（后续 save_state 才能正常覆盖）
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        # 恢复 chapters 正文（备份里有的正文文件全部还原）
        bak_chapters = backup_dir / "chapters"
        if bak_chapters.is_dir():
            dst_chapters = p.parent.parent / "chapters"
            for src_f in bak_chapters.rglob("*"):
                if src_f.is_file():
                    rel = src_f.relative_to(bak_chapters)
                    dst_f = dst_chapters / rel
                    dst_f.parent.mkdir(parents=True, exist_ok=True)
                    _sh.copy2(src_f, dst_f)
        print(f"  [恢复] novel 项目从备份自动恢复: {project_name}（state + 正文）")
        return data
    except Exception:
        return None

def save_state(path, data, caller="auto"):
    """保存 state 并验证核心规划字段完整性"""
    state_file = Path(path)
    old_data = None
    if state_file.exists():
        old_data = json.loads(state_file.read_text(encoding="utf-8-sig"))

    # ── 核心字段完整性保护 ──
    # plan-chapter 是合法核心字段写入入口（注册子结构，内部已有新角色检测 + writing_prompt
    # 硬校验 + summary 字数校验），跳过指纹拦截——否则多章规划时每章注册都会因"新增了
    # 上一章没有的子结构"导致哈希变化而误阻断（指纹只应拦"绕过管道的非法修改"）。
    fp_path = _fingerprint_path(path)
    if fp_path.exists() and caller not in ("plan-chapter", "replan-novel-sub", "replan-novel-chapter", "novel-confirm"):
        expected_fp = fp_path.read_text(encoding="utf-8").strip()
        current_fp = _fingerprint(data)
        if current_fp != expected_fp:
            print(f"[HOOK-BLOCK] 核心规划字段被非法修改！指纹不匹配")
            print(f"  允许修改的字段: word_count, status, continuity_notes, style_check_notes, timeline, entity_tracker, behavior_summary")
            print(f"  禁止修改: title, overview, summary, tone, 角色核心信息, novel_info, writing_style, setting")
            print(f"  来源: {caller}")
            # 恢复旧数据中的运行时字段到新数据
            if old_data is not None:
                _merge_runtime_fields(data, old_data)
                new_fp = _fingerprint(data)
                if new_fp == expected_fp:
                    print(f"  [自动修复] 运行时字段已合并，指纹恢复")
                else:
                    print(f"  [HOOK-BLOCK] 无法自动修复，拒绝写入。指纹仍不匹配")
                    print(f"  期望: {expected_fp}")
                    print(f"  实际: {new_fp}")
                    sys.exit(1)

    # 首次写入时记录指纹（仅当至少有子结构注册完成，由 plan-chapter 触发）
    if (not fp_path.exists() and old_data is not None 
        and any(ch.get("sub_structures") for ch in old_data.get("chapters", []))):
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        sub_count = sum(len(ch.get("sub_structures", {})) for ch in old_data.get("chapters", []))
        fp_path.write_text(_fingerprint(old_data), encoding="utf-8")
        print(f"  [指纹] 已记录核心规划字段指纹（{sub_count} 个子结构）")

    # ── 原子写入：写 .tmp → fsync → replace（覆盖） ──
    # Windows 上 rename 目标已存在时抛 WinError 183（os.rename 不覆盖），
    # 必须用 replace（os.replace）才能覆盖已存在的 novel_state.json
    tmp_path = state_file.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 验证写入的 JSON 可重新解析
    try:
        json.loads(tmp_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        tmp_path.unlink(missing_ok=True)
        print(f"[HOOK-BLOCK] save_state: 生成的 JSON 非法，拒绝写入")
        sys.exit(1)
    tmp_path.replace(state_file)

    # ── 备份（防项目丢失无法续写）：每次成功保存后同步一份到 data/novel/backups/ ──
    try:
        _backup_state(state_file, data)
    except Exception:
        pass

    # plan-chapter / replan-novel-sub / replan-novel-chapter / novel-confirm 成功后更新指纹，使后续写入不被误阻断
    if caller in ("plan-chapter", "replan-novel-sub", "replan-novel-chapter", "novel-confirm"):
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        new_fp = _fingerprint(data)
        fp_path.write_text(new_fp, encoding="utf-8")
        sub_count = sum(len(ch.get("sub_structures", {})) for ch in data.get("chapters", []))
        print(f"  [指纹] 已更新（{sub_count} 个子结构）")


def _backup_state(state_file: Path, data: dict):
    """同步项目备份到 data/novel/backups/<项目名>/（state + chapters 正文）。

    项目目录被误删/损坏时，从备份完整恢复即可续写（角色/设定/实体/命题框 + 已写正文都不丢）。
    备份目录在 projects 的兄弟级（data/novel/backups/）——若备份落在 projects 内，
    删 projects 时备份会一起被删，失去意义。
    备份命名含项目名，覆盖写（只留最新一份），不累积历史版本。
    """
    project_dir = state_file.parent.parent          # .../projects/<name>/
    project_name = project_dir.name
    if not project_name or project_name == "projects":
        return
    backup_dir = project_dir.parent.parent / "backups" / project_name   # data/novel/backups/<name>/
    backup_dir.mkdir(parents=True, exist_ok=True)
    # 1) state 文件（原子写）
    bak = backup_dir / "novel_state.json"
    tmp = bak.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(bak)
    # 2) chapters 正文目录（整目录同步——已写正文也备份，防正文丢失）
    src_chapters = project_dir / "chapters"
    if src_chapters.is_dir():
        import shutil as _sh
        dst_chapters = backup_dir / "chapters"
        dst_chapters.mkdir(parents=True, exist_ok=True)
        for src_f in src_chapters.rglob("*"):
            if src_f.is_file():
                rel = src_f.relative_to(src_chapters)
                dst_f = dst_chapters / rel
                dst_f.parent.mkdir(parents=True, exist_ok=True)
                # 有变化才复制（正文通常只增不删，直接覆盖写最新）
                if not dst_f.exists() or dst_f.stat().st_size != src_f.stat().st_size:
                    _sh.copy2(src_f, dst_f)


def _merge_runtime_fields(new_data: dict, old_data: dict):
    """将旧数据的运行时字段合并到新数据，修复指纹"""
    # 1) 结构清理：删除新数据中多余的章节/子结构
    old_ch_ids = {ch["id"] for ch in old_data.get("chapters", [])}
    new_data["chapters"] = [ch for ch in new_data.get("chapters", []) if ch["id"] in old_ch_ids]

    # 2) 合并运行时字段
    for new_ch in new_data.get("chapters", []):
        for old_ch in old_data.get("chapters", []):
            if new_ch["id"] != old_ch["id"]:
                continue
            new_ch["word_count"] = old_ch.get("word_count", 0)
            new_ch["status"] = old_ch.get("status", "pending")
            new_ch["continuity_notes"] = old_ch.get("continuity_notes", [])
            new_ch["style_check_notes"] = old_ch.get("style_check_notes", [])
            new_ch["behavior_summary"] = old_ch.get("behavior_summary", {})
            # 子结构: 还原规划字段 + 保留运行时字段
            old_subs = old_ch.get("sub_structures", {})
            if old_subs:
                for sk, sv in new_ch.get("sub_structures", {}).items():
                    old_sv = old_subs.get(sk)
                    if old_sv is None:
                        # 新数据中的多余子结构 → 删除
                        continue
                    sv["word_count"] = old_sv.get("word_count", 0)
                    sv["status"] = old_sv.get("status", "pending")
                # 删除不在 old_subs 中的子结构
                new_ch["sub_structures"] = {k: v for k, v in new_ch.get("sub_structures", {}).items() if k in old_subs}
            break
    # 3) 元数据
    new_data["meta"] = old_data.get("meta", {})
    # 4) 时间线
    if "timeline" in old_data:
        new_data["timeline"] = old_data["timeline"]
    # 5) 实体追踪器（纯运行时数据，全量保留）
    if "entity_tracker" in old_data:
        new_data.setdefault("entity_tracker", {})
        for key in old_data["entity_tracker"]:
            new_data["entity_tracker"][key] = old_data["entity_tracker"][key]
    # 6) 签名
    if "signature" in old_data:
        new_data["signature"] = old_data["signature"]

def add_char(path, name, role_attr, first_appearance, traits="", mbti="", archetype="", function="", aliases=""):
    data = load_state(path)
    # ── 规划阻断：有 first_appearance 则必须同时填写 function ──
    if first_appearance and not function:
        print(f"[HOOK-BLOCK] 角色 \"{name}\" 设置了 first_appearance 但未填写 function")
        print(f"[要求] 请补充 function 参数描述该角色的功能定位，例如：")
        print(f"  \"推荐主角购买《实用擒拿格斗术》，关键信息提供者\"")
        print(f"  用法: python novel_state_manager.py add-char <state_path> \"{name}\" \"{role_attr}\" \"{first_appearance}\" \"{traits}\" \"{mbti}\" \"{archetype}\" \"<功能描述>\"")
        sys.exit(1)
    chars = data.get("characters", [])
    for c in chars:
        if c.get("name") == name:
            if role_attr: c["role"] = role_attr
            if first_appearance: c["first_appearance"] = first_appearance
            if traits: c["traits"] = [t.strip() for t in traits.split(",")]
            if mbti: c["mbti"] = mbti
            if archetype: c["archetype"] = archetype
            if function: c["function"] = function
            if aliases: c["aliases"] = [a.strip() for a in aliases.split(",")]
            save_state(path, data, caller="add-char")
            print(f"[角色更新] {name} (MBTI={mbti or '无'}, 原型={archetype or '无'})")
            if aliases:
                print(f"[别名] {name} 别名: {aliases}")
            return
    entry = {"name": name, "role": role_attr, "first_appearance": first_appearance}
    if traits:
        entry["traits"] = [t.strip() for t in traits.split(",")]
    if mbti:
        entry["mbti"] = mbti
    if archetype:
        entry["archetype"] = archetype
    if function:
        entry["function"] = function
    if aliases:
        entry["aliases"] = [a.strip() for a in aliases.split(",")]
    chars.append(entry)
    data["characters"] = chars
    save_state(path, data, caller="add-char")
    print(f"[角色新增] {name} (出场: {first_appearance}, MBTI={mbti or '无'}, 原型={archetype or '无'})")
    if aliases:
        print(f"[别名] {name} 别名: {aliases}")

def register_alias(path, char_name, alias):
    """为角色注册单个别名。由 atomic_writer 在检测到【别名】声明时调用。"""
    data = load_state(path)
    for c in data.get("characters", []):
        if c.get("name") != char_name:
            continue
        existing = c.get("aliases", [])
        if not isinstance(existing, list):
            existing = []
        if alias in existing:
            print(f"[别名] {char_name} 已存在别名「{alias}」，无需重复注册")
            return
        existing.append(alias)
        c["aliases"] = existing
        save_state(path, data, caller="register-alias")
        print(f"[别名] {char_name} ← 「{alias}」")
        return
    print(f"[WARN] 角色「{char_name}」不存在，无法注册别名")

def update_sub(path, chapter, sub_key, word_count):
    """
    即时标记子结构完成（非批量，非延迟）
    每次调用立即写入 novel_state.json
    """
    data = load_state(path)
    for ch in data.get("chapters", []):
        if ch["id"] != chapter:
            continue
        if "sub_structures" not in ch:
            ch["sub_structures"] = {}
        prev_wc = ch["sub_structures"].get(sub_key, {}).get("word_count", 0)
        prev = ch["sub_structures"].get(sub_key, {})
        prev["word_count"] = int(word_count)
        prev["status"] = "completed"
        ch["sub_structures"][sub_key] = prev
        # 更新章总字数（减去旧字数+新字数）
        ch["word_count"] = ch.get("word_count", 0) - prev_wc + int(word_count)
        break
    save_state(path, data, caller="update-sub")
    print(f"[SUB-COMPLETE] {chapter}{sub_key}: {word_count}字, status=completed")

def finalize_chapter(path, chapter):
    data = load_state(path)
    for ch in data.get("chapters", []):
        if ch["id"] == chapter:
            ch["status"] = "completed"
            break
    save_state(path, data, caller="finalize-chapter")
    print(f"[章节] {chapter} [OK] 完成")

def add_timeline(path, time_point, event):
    data = load_state(path)
    tl = data.get("timeline", [])
    tl.append({"event": event, "time_point": time_point})
    data["timeline"] = tl
    save_state(path, data, caller="add-timeline")
    print(f"[时间线] {time_point}: {event}")

def set_signature(path, enabled, text=""):
    """设置署名开关和文本。代码级强制，LLM 不可自行添加。"""
    data = load_state(path)
    enabled_bool = enabled.lower() in ("true", "1", "yes")
    data["signature"] = {"enabled": enabled_bool, "text": text}
    save_state(path, data, caller="set-signature")
    status = "开" if enabled_bool else "关"
    print(f"[署名] signature.enabled={enabled_bool} ({status})")
    if enabled_bool and text:
        print(f"[署名] signature.text=\"{text}\"")
    elif enabled_bool:
        print(f"[署名] signature.text 为空（默认不显示署名行）")
    if not enabled_bool:
        print(f"[署名] 已关闭，LLM 不得在正文中写入任何署名/代名内容（atomic_writer 代码级阻断）")


def init_project(name, project_name, length="medium", num_chapters=None):
    """
    初始化 novel_state.json 骨架。
    name: 项目名（自动创建在 projects/<name>/data/）或完整路径
    length: short/medium/long（默认 medium）
    num_chapters: 不传则按 length 范围取中值
    """
    p = Path(name)
    # 如果名字不含路径分隔符，视为项目名，自动创建标准化子目录
    if "/" not in name and "\\" not in name:
        base = DATA_DIR
        p = base / name / "data" / "novel_state.json"
        print(f"[初始化] 项目目录: {p.parent}")
    if p.exists():
        print(f"[HOOK-BLOCK] {p} 已存在，禁止重复初始化")
        sys.exit(1)
    if length not in LENGTH_CHAPTERS:
        print(f"[HOOK-BLOCK] 无效篇幅: {length}，可选: short/medium/long")
        sys.exit(1)
    if num_chapters is None:
        lo, hi = LENGTH_CHAPTERS[length]
        num_chapters = (lo + hi) // 2
    else:
        num_chapters = int(num_chapters)
        lo, hi = LENGTH_CHAPTERS[length]
        if num_chapters < lo or num_chapters > hi:
            print(f"[HOOK-BLOCK] {LENGTH_LABELS[length]} 章数应在 {lo}-{hi} 之间，收到 {num_chapters}")
            sys.exit(1)
    today = datetime.now().strftime("%Y-%m-%d")
    chapters = []
    for i in range(1, num_chapters + 1):
        chapters.append({
            "id": f"L{i:02d}",
            "title": f"第{i}章",
            "overview": "",
            "word_count": 0,
            "status": "pending",
            "sub_structures": {}
        })
    # 末章标记
    if chapters:
        chapters[-1]["is_final"] = True
    data = {
        "project": project_name,
        "created": today,
        "meta": {
            "current_phase": "stage1_init",
            "version": "1.12.6",
            "length": length
        },
        "writing_style": {
            "narrative_voice": "",
            "tense": "",
            "sentence_preference": "",
            "vocabulary_register": "",
            "description_depth": "",
            "custom_rules": ""
        },
        "characters": [],
        "entity_tracker": {"entities": [], "relations": []},
        "chapters": chapters,
        "timeline": [],
        "signature": {"enabled": False, "text": ""}
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[初始化] {project_name} → {p}")
    print(f"[初始化] 篇幅: {LENGTH_LABELS.get(length, length)}（{num_chapters} 章）")
    print(f"[初始化] 当前阶段: stage1_init")
    print(f"[下一步] 设置场景配置和大纲后:")
    print(f"  python novel_causality_check.py outline <state_path>")
    print(f"  python novel_pipeline_gate.py set-phase <state_path> stage1_done")

if __name__ == "__main__":
    # list-projects 不需要 state_path，优先处理
    if len(sys.argv) >= 2 and sys.argv[1] == "list-projects":
        cmd = "list-projects"
    elif len(sys.argv) < 3:
        print("用法: python novel_state_manager.py <命令> <state_path> [args...]")
        print("  命令:")
        print("    init       <项目名> [length] [num_chapters]  初始化新小说")
        print("                   length: short(3-6章), medium(8-10章,默认), long(11章+)")
        print("                   也可传入完整路径: init ./my/path/data/novel_state.json 小说名")
        print("    add-char   <name> <role> <first_appearance> [traits] [mbti] [archetype] [function] [aliases]")
        print("    register-alias <char_name> <alias>                    为角色注册别名")
        print("    update-sub <chapter> <sub_key> <word_count>")
        print("    finalize   <chapter>")
        print("    add-timeline <time_point> <event>")
        print("    set-signature <true|false> [text]")
        print("    set-length    <short|medium|long>")
        print("    list-projects                     列出所有已创建的项目")
        print("")
        print("  示例:")
        print("    python novel_state_manager.py init my-novel 我的小说   # 自动创建")
        print("    python novel_state_manager.py init ./path/state.json 小说名  # 指定路径")
        print("    python novel_state_manager.py register-alias ./path/state.json 老陈 陈叔")
        sys.exit(1)
        print("    update-sub <chapter> <sub_key> <word_count>")
        print("    finalize   <chapter>")
        print("    add-timeline <time_point> <event>")
        print("    set-signature <true|false> [text]")
        print("    set-length    <short|medium|long>")
        print("    list-projects                     列出所有已创建的项目")
        print("    list-projects                     列出所有已创建的项目")
        print("")
        print("  示例:")
        print("    python novel_state_manager.py init my-novel 我的小说   # 自动创建")
        print("    python novel_state_manager.py init ./path/state.json 小说名  # 指定路径")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "list-projects":
        from _path_utils import list_projects
        projects = list_projects()
        if not projects:
            print("没有找到已创建的项目。")
        else:
            print(f"\n{'='*55}")
            print(f"  已创建的项目 ({len(projects)}):")
            print(f"{'='*55}")
            for p in projects:
                print(f"  📖 {p['name']}")
                print(f"    路径: {p['path']}")
                print(f"    篇幅: {p['length']} | 阶段: {p['phase']}")
                print(f"    章节: {p['done']}/{p['chapters']}")
                print()
        sys.exit(0)
    sp = sys.argv[2]
    if cmd == "add-char":
        add_char(sp, sys.argv[3], sys.argv[4], sys.argv[5],
                 sys.argv[6] if len(sys.argv) > 6 else "",
                 sys.argv[7] if len(sys.argv) > 7 else "",
                 sys.argv[8] if len(sys.argv) > 8 else "",
                 sys.argv[9] if len(sys.argv) > 9 else "",
                 sys.argv[10] if len(sys.argv) > 10 else "")
    elif cmd == "update-sub":
        update_sub(sp, sys.argv[3], sys.argv[4], sys.argv[5])
    elif cmd == "finalize":
        finalize_chapter(sp, sys.argv[3])
    elif cmd == "add-timeline":
        add_timeline(sp, sys.argv[3], sys.argv[4])
    elif cmd == "set-signature":
        text = sys.argv[4] if len(sys.argv) > 4 else ""
        set_signature(sp, sys.argv[3], text)
    elif cmd == "set-length":
        length = sys.argv[3]
        data = load_state(sp)
        if length not in LENGTH_CHAPTERS:
            print(f"[HOOK-BLOCK] 无效篇幅: {length}，可选: short/medium/long")
            sys.exit(1)
        data.setdefault("meta", {})["length"] = length
        save_state(sp, data, caller="set-length")
        print(f"[篇幅] 已设为 {LENGTH_LABELS[length]}")
    elif cmd == "init":
        # sys.argv[2] = 项目名（可含路径）; sys.argv[3] = 显示名; [4]=length; [5]=num
        display_name = sys.argv[3] if len(sys.argv) > 3 else sys.argv[2]
        length = sys.argv[4] if len(sys.argv) > 4 else "medium"
        num = sys.argv[5] if len(sys.argv) > 5 else None
        init_project(sys.argv[2], display_name, length, num)
    elif cmd == "register-alias":
        register_alias(sp, sys.argv[3], sys.argv[4])
    else:
        print(f"[错误] 未知命令: {cmd}")
