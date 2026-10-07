"""Coverage for ci_playbook_invocations.py.

Every deploy gate reads its `ansible-playbook` calls through parse_invocations,
so a shape it drops silently shrinks each of those gates.
"""

from __future__ import annotations

from conftest import load_script

mod = load_script("ci_playbook_invocations.py")
parse = mod.parse_invocations


class TestPlaybookAndInventory:
    def test_the_playbook_is_found_after_a_value_flag(self):
        """The shape a positional regex misses."""
        (call,) = parse('ansible-playbook -i prod -e "x=1" site.yml')
        assert call["inventory"] == "prod"
        assert call["playbook"] == "site.yml"
        assert call["argv"] == "-i prod -e x=1 site.yml"

    def test_a_call_without_an_inventory_keeps_none(self):
        assert parse("ansible-playbook site.yml")[0]["inventory"] is None

    def test_an_attached_short_inventory_flag_is_parsed(self):
        assert parse("ansible-playbook -iprod site.yml")[0]["inventory"] == "prod"

    def test_an_attached_long_inventory_flag_is_parsed(self):
        assert parse("ansible-playbook --inventory=prod site.yml")[0]["inventory"] == "prod"

    def test_an_attached_extra_vars_flag_is_not_a_playbook(self):
        (call,) = parse("ansible-playbook -i prod -evars.yml site.yml")
        assert call["playbook"] == "site.yml"

    def test_a_yaml_suffixed_playbook_is_found(self):
        assert parse("ansible-playbook -i prod site.yaml")[0]["playbook"] == "site.yaml"

    def test_an_unbalanced_quote_still_yields_the_playbook(self):
        assert parse("bash -c 'ansible-playbook -i prod site.yml")[0]["playbook"] == "site.yml"

    def test_a_call_with_no_playbook_is_dropped(self):
        assert parse("ansible-playbook --version") == []


class TestSeveralCalls:
    def test_two_chained_calls_on_one_line_are_two_invocations(self):
        """A parser keeping only the first call leaves the second deploy step
        unchecked."""
        parsed = parse(
            "op run -- ansible-playbook -i prod a.yml && "
            "op run -- ansible-playbook -i prod b.yml"
        )
        assert [c["playbook"] for c in parsed] == ["a.yml", "b.yml"]

    def test_a_semicolon_separated_pair_is_two_invocations(self):
        parsed = parse("ansible-playbook a.yml; ansible-playbook b.yml")
        assert [c["playbook"] for c in parsed] == ["a.yml", "b.yml"]

    def test_a_pipe_ends_the_argv(self):
        (call,) = parse("ansible-playbook -i prod a.yml | tee log")
        assert call["playbook"] == "a.yml"
        assert "tee" not in call["argv"]

    def test_a_backslash_continued_call_is_one_invocation(self):
        parsed = parse("ansible-playbook -i prod \\\n    site.yml --tags base")
        assert len(parsed) == 1
        assert parsed[0]["playbook"] == "site.yml"
        assert parsed[0]["tags"] == {"base"}


class TestLimit:
    def test_every_limit_spelling_is_captured(self):
        for text in (
            "ansible-playbook -i prod site.yml --limit proxmox",
            "ansible-playbook -i prod site.yml --limit=proxmox",
            "ansible-playbook -i prod site.yml -l proxmox",
            "ansible-playbook -i prod site.yml -lproxmox",
        ):
            assert parse(text)[0]["limit"] == "proxmox", text


class TestTags:
    def test_a_tags_list_becomes_a_set(self):
        assert parse("ansible-playbook site.yml --tags base,ssh")[0]["tags"] == {"base", "ssh"}

    def test_the_short_tags_flag_is_captured(self):
        assert parse("ansible-playbook site.yml -t base")[0]["tags"] == {"base"}

    def test_repeated_tags_flags_union(self):
        assert parse("ansible-playbook site.yml --tags a --tags b")[0]["tags"] == {"a", "b"}

    def test_skip_tags_are_kept_out_of_tags(self):
        """A --skip-tags value read as a selection makes the gate assert the
        opposite of what the job does."""
        (call,) = parse("ansible-playbook site.yml --tags base --skip-tags slow")
        assert call["tags"] == {"base"}
        assert call["skip_tags"] == {"slow"}

    def test_a_call_with_no_tags_reports_none(self):
        (call,) = parse("ansible-playbook site.yml")
        assert call["tags"] is None and call["skip_tags"] is None

    def test_a_skip_tags_value_is_not_read_as_the_playbook(self):
        (call,) = parse("ansible-playbook --skip-tags other.yml site.yml")
        assert call["playbook"] == "site.yml"
