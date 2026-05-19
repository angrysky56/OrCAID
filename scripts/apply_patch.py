#!/usr/bin/env python3
"""Apply OrCAID generated patches to a target git repository branch cleanly.

This script parses a generated `patch.diff`, filters out metadata comments
that can cause git to reject the patch, checks out or creates a target branch
in the destination repository, applies the patch, and commits the changes.
"""

import argparse
import os
import subprocess
import sys
import tempfile


def run_cmd(cmd, cwd=None, capture=True):
    """Run a system command and return output or exit code."""
    try:
        if capture:
            res = subprocess.run(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            return res.stdout.strip(), None
        else:
            res = subprocess.run(cmd, cwd=cwd, check=True)
            return "", None
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() if e.stderr else str(e)
        return "", err


def clean_patch(patch_path):
    """Clean the patch by removing metadata comments (lines starting with #)."""
    if not os.path.exists(patch_path):
        print(f"Error: Patch file '{patch_path}' does not exist.")
        sys.exit(1)

    cleaned_lines = []
    with open(patch_path, "r", encoding="utf-8") as f:
        for line in f:
            # Ignore leading comments that can cause "corrupt patch" errors in git
            if not cleaned_lines and line.startswith("#"):
                continue
            cleaned_lines.append(line)

    return "".join(cleaned_lines)


def main():
    parser = argparse.ArgumentParser(
        description="Clean, apply, and commit an OrCAID patch onto any target git repository branch."
    )
    parser.add_argument(
        "--patch",
        "-p",
        required=True,
        help="Path to the generated patch.diff file (e.g. outputs/.../patch.diff)"
    )
    parser.add_argument(
        "--repo-dir",
        "-d",
        default=".",
        help="Path to the target repository directory (defaults to current directory)"
    )
    parser.add_argument(
        "--branch",
        "-b",
        default="orcaid-patch",
        help="Name of the git branch to create or checkout (defaults to 'orcaid-patch')"
    )
    parser.add_argument(
        "--commit-message",
        "-m",
        default="feat: apply OrCAID multi-agent refactoring patch",
        help="Commit message to use when committing the patch"
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force apply patch even if target repository has uncommitted changes"
    )

    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)
    if not os.path.exists(os.path.join(repo_dir, ".git")):
        print(f"Error: Target directory '{repo_dir}' is not a git repository.")
        sys.exit(1)

    print("=" * 60)
    print("OrCAID Patch Applicator")
    print("=" * 60)
    print(f"Target Repository: {repo_dir}")
    print(f"Source Patch:      {args.patch}")
    print(f"Target Branch:     {args.branch}")
    print("-" * 60)

    # Step 1: Check git status for uncommitted changes
    status_out, err = run_cmd(["git", "status", "--porcelain"], cwd=repo_dir)
    if err:
        print(f"Error checking git status: {err}")
        sys.exit(1)

    if status_out and not args.force:
        print("Warning: Target repository has uncommitted changes:")
        print(status_out)
        print("Please commit or stash your changes before applying, or use --force.")
        sys.exit(1)

    # Step 2: Clean the patch metadata
    print("[1/4] Cleaning patch metadata...")
    cleaned_diff = clean_patch(args.patch)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as temp_patch:
        temp_patch.write(cleaned_diff)
        temp_patch_path = temp_patch.name

    try:
        # Step 3: Checkout or create the target branch
        print(f"[2/4] Checking out target branch '{args.branch}'...")
        # Check if branch exists
        _, branch_err = run_cmd(["git", "show-ref", "--verify", f"refs/heads/{args.branch}"], cwd=repo_dir)
        if branch_err:
            # Create new branch
            print(f"      Branch '{args.branch}' does not exist. Creating it...")
            _, err = run_cmd(["git", "checkout", "-b", args.branch], cwd=repo_dir)
        else:
            # Switch to existing branch
            print(f"      Switching to existing branch '{args.branch}'...")
            _, err = run_cmd(["git", "checkout", args.branch], cwd=repo_dir)

        if err:
            print(f"Error checking out branch: {err}")
            sys.exit(1)

        # Step 4: Apply the patch cleanly
        print("[3/4] Applying OrCAID patch to working directory...")
        _, apply_err = run_cmd(
            ["git", "apply", "--whitespace=fix", temp_patch_path],
            cwd=repo_dir
        )
        if apply_err:
            print(f"Error applying patch: {apply_err}")
            sys.exit(1)

        # Step 5: Stage and commit the applied changes
        print("[4/4] Committing applied changes...")
        _, err = run_cmd(["git", "add", "-A"], cwd=repo_dir)
        if err:
            print(f"Error staging files: {err}")
            sys.exit(1)

        commit_out, err = run_cmd(["git", "commit", "-m", args.commit_message], cwd=repo_dir)
        if err:
            print(f"Error committing patch: {err}")
            sys.exit(1)

        # Get the new commit hash
        commit_hash, _ = run_cmd(["git", "rev-parse", "--short", "HEAD"], cwd=repo_dir)

        print("-" * 60)
        print("Success! OrCAID patch successfully applied and committed.")
        print(f"Commit Hash:   {commit_hash}")
        print(f"Branch:        {args.branch}")
        print(f"Message:       {args.commit_message}")
        print("=" * 60)

    finally:
        # Clean up temporary patch file
        if os.path.exists(temp_patch_path):
            os.remove(temp_patch_path)


if __name__ == "__main__":
    main()
