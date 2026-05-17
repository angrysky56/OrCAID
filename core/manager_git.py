class GitMixin:
    """Mixin for git-related operations in the Manager."""

    def stash_if_dirty(self) -> bool:
        """
        Stash uncommitted changes in the main repository if it's dirty.

        Returns:
            bool: True if changes were stashed, False otherwise.
        """
        status_result = self.workspace.execute_command(
            f"cd {self.repo_dir} && git status --porcelain", timeout=30
        )
        if status_result.exit_code == 0 and status_result.stdout.strip():
            self.log("Stashing uncommitted changes in main repo before merge...")
            stash_result = self.workspace.execute_command(
                f"cd {self.repo_dir} && git stash", timeout=30
            )
            if stash_result.exit_code == 0 and "No local changes" not in (
                stash_result.stdout or ""
            ):
                return True
        return False

    def unstash(self) -> None:
        """Restore stashed changes in the main repository."""
        self.log("Restoring stashed changes...")
        self.workspace.execute_command(
            f"cd {self.repo_dir} && git stash pop", timeout=30
        )

    def merge_branch(
        self, branch_name: str, force_theirs: bool = False
    ) -> tuple[bool, str, list[str]]:
        """
        Merge a branch into the current branch.

        Args:
            branch_name: The name of the branch to merge.
            force_theirs: Whether to force resolution using 'theirs' strategy on conflicts.

        Returns:
            tuple: (success: bool, message: str, conflict_files: list[str])
        """
        self.log(f"Merging branch {branch_name}...")

        stashed = False
        if self.task.should_stash_before_merge:
            stashed = self.stash_if_dirty()

        merge_cmd = f"cd {self.repo_dir} && " f"git merge {branch_name} --no-edit"
        result = self.workspace.execute_command(merge_cmd, timeout=60)

        if result.exit_code == 0:
            self.log(f"Successfully merged {branch_name}")
            if stashed:
                self.unstash()
            return True, "Merged successfully", []

        # Check if it's a conflict
        error_msg = result.stderr or result.stdout or "Unknown error"
        is_conflict = "CONFLICT" in error_msg or "conflict" in error_msg.lower()

        if is_conflict:
            # Extract conflicted file names before aborting
            conflict_cmd = f"cd {self.repo_dir} && git diff --name-only --diff-filter=U"
            conflict_result = self.workspace.execute_command(conflict_cmd, timeout=30)
            conflict_files = (
                [
                    f.strip()
                    for f in conflict_result.stdout.strip().split("\n")
                    if f.strip()
                ]
                if conflict_result.exit_code == 0
                else []
            )

            self.log(
                f"Merge conflict detected for {branch_name}, files: {conflict_files}"
            )

            abort_cmd = f"cd {self.repo_dir} && git merge --abort"
            self.workspace.execute_command(abort_cmd, timeout=30)

            if force_theirs:
                self.log(
                    "Force-resolving with --strategy-option theirs (engineer has no rounds left)..."
                )
                merge_theirs_cmd = (
                    f"cd {self.repo_dir} && "
                    f"git merge {branch_name} --no-edit -X theirs"
                )
                result = self.workspace.execute_command(merge_theirs_cmd, timeout=60)

                if result.exit_code == 0:
                    self.log(f"Successfully merged {branch_name} using theirs strategy")
                    if stashed:
                        self.unstash()
                    return (
                        True,
                        "Merged successfully (used theirs strategy for conflicts)",
                        [],
                    )

                error_msg = result.stderr or result.stdout or "Unknown error"
                self.log(f"Merge with theirs strategy also failed: {error_msg[:200]}")

                abort_cmd = f"cd {self.repo_dir} && git merge --abort"
                self.workspace.execute_command(abort_cmd, timeout=30)

                if stashed:
                    self.unstash()
                return (
                    False,
                    f"Merge failed even with conflict resolution: {error_msg[:200]}",
                    [],
                )

            if stashed:
                self.unstash()
            return (
                False,
                f"Merge conflict in files: {', '.join(conflict_files)}",
                conflict_files,
            )

        # Non-conflict error
        self.log(f"Warning: Merge failed for {branch_name}: {error_msg[:200]}")
        if stashed:
            self.unstash()
        return False, f"Merge failed: {error_msg[:200]}", []

    def get_uncommitted_changes(self, worktree_path: str) -> list[str]:
        """
        Get a list of uncommitted changes in a worktree.

        Args:
            worktree_path: Path to the worktree.

        Returns:
            list: List of modified file paths.
        """
        if not worktree_path:
            return []

        status_cmd = f"cd {worktree_path} && git status --porcelain"
        result = self.workspace.execute_command(status_cmd, timeout=30)

        if result.exit_code != 0 or not result.stdout.strip():
            return []

        modified_files = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                parts = line.split(maxsplit=1)
                if len(parts) >= 2:
                    file_path = parts[1].strip()
                    modified_files.append(file_path)

        return modified_files

    def commit_worktree_changes(
        self, worktree_path: str, branch_name: str, engineer_id: str, task_id: str
    ) -> tuple[bool, str, list[str]]:
        """
        Commit uncommitted changes in a worktree and merge them into the main branch.

        Args:
            worktree_path: Path to the worktree.
            branch_name: Name of the branch in the worktree.
            engineer_id: ID of the engineer who made the changes.
            task_id: ID of the task being implementation.

        Returns:
            tuple: (success: bool, message: str, committed_files: list[str])
        """
        self.log(f"Committing uncommitted changes in worktree for {engineer_id}...")

        modified_files = self.get_uncommitted_changes(worktree_path)
        if not modified_files:
            self.log("No uncommitted changes found in worktree")
            return False, "No uncommitted changes to commit", []

        self.log(f"Found {len(modified_files)} uncommitted files: {modified_files}")

        git_config_cmd = (
            f"cd {worktree_path} && "
            f'git config user.name "openhands" && '
            f'git config user.email "openhands@all-hands.dev"'
        )
        self.workspace.execute_command(git_config_cmd, timeout=30)

        add_cmd = f"cd {worktree_path} && git add ."
        add_result = self.workspace.execute_command(add_cmd, timeout=60)
        if add_result.exit_code != 0:
            self.log(f"Failed to stage changes: {add_result.stderr}")
            return False, f"Failed to stage changes: {add_result.stderr}", []

        commit_message = f"Partial implementation from {engineer_id} ({task_id})"
        commit_cmd = f'cd {worktree_path} && git commit -m "{commit_message}"'
        commit_result = self.workspace.execute_command(commit_cmd, timeout=60)

        if commit_result.exit_code != 0:
            error_output = commit_result.stderr or commit_result.stdout or ""
            if "nothing to commit" in error_output:
                self.log("No changes to commit (files may be identical)")
                return False, "No changes to commit", []
            self.log(f"Failed to commit in worktree: {error_output}")
            return False, f"Failed to commit: {error_output[:200]}", []

        self.log(f"Committed changes in worktree branch {branch_name}")

        merge_success, merge_message, _ = self.merge_branch(
            branch_name, force_theirs=True
        )

        if merge_success:
            self.log(f"Successfully merged worktree changes: {merge_message}")
            return (
                True,
                f"Committed and merged {len(modified_files)} files",
                modified_files,
            )
        else:
            self.log(f"Merge failed after committing: {merge_message}")
            return False, f"Committed but merge failed: {merge_message}", modified_files
