"""
Superset MCP Agentic Pipeline — Entry Point.
 
Usage:
    python main.py                  Launch the TUI
    python main.py --query "..."    Run a single query (headless)
    python main.py --health         Run health check only
"""
 
import argparse
import logging
import sys
import os
 
# Add the agent directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
 
 
def setup_logging(verbose: bool = False):
    """Configure logging — file-based when TUI is active, stderr when headless."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.FileHandler(
                os.path.join(os.path.dirname(__file__), "agent.log"),
                mode="a",
            ),
        ],
    )
 
 
def run_health_check():
    """Run health check only and print results."""
    from pipeline import Pipeline
 
    print("🔍 Running health checks...\n")
    pipeline = Pipeline()
 
    try:
        result = pipeline.health_check()
        if result.success:
            print("✅ All health checks passed!")
            print("   • MCP service: OK")
            print("   • Superset API: OK")
        else:
            print(f"❌ Health check failed: {result.error}")
            sys.exit(1)
    finally:
        pipeline.close()
 
 
def run_headless(query: str):
    """Run a single query without the TUI."""
    from pipeline import Pipeline
 
    print(f"🚀 Running pipeline for: \"{query}\"\n")
    pipeline = Pipeline()
 
    try:
        report = pipeline.run(query)
 
        print("\n" + "=" * 60)
        if report.success:
            print(f"✅ Dashboard: {report.dashboard_url}")
            print(f"   Charts: {sum(1 for c in report.charts_created if c.success)}/{len(report.charts_created)}")
        else:
            print("❌ Pipeline failed")
 
        if report.errors:
            print(f"\n⚠ Errors ({len(report.errors)}):")
            for err in report.errors:
                print(f"   • {err}")
 
        if report.charts_created:
            print(f"\n📊 Charts:")
            for cr in report.charts_created:
                icon = "✅" if cr.success else "❌"
                print(f"   {icon} {cr.spec.name} ({cr.spec.chart_type})"
                      + (f" → id={cr.chart_id}" if cr.chart_id else f" — {cr.error}"))
 
        print("=" * 60)
 
    finally:
        pipeline.close()
 
 
def run_tui():
    """Launch the interactive TUI."""
    from tui import TUI
    app = TUI()
    app.run()
 
 
def main():
    parser = argparse.ArgumentParser(
        description="Superset MCP Agentic Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                                           # Launch TUI
  python main.py --query "show deposits vs withdrawals"    # Headless mode
  python main.py --health                                  # Check connectivity
  python main.py --query "top banks by volume" --verbose   # With debug logs
        """,
    )
    parser.add_argument("--query", "-q", type=str, help="Run a single query (headless mode)")
    parser.add_argument("--health", action="store_true", help="Run health check only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
 
    args = parser.parse_args()
    setup_logging(args.verbose)
 
    if args.health:
        run_health_check()
    elif args.query:
        run_headless(args.query)
    else:
        run_tui()
 
 
if __name__ == "__main__":
    main()