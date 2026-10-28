"""Run the installed managed lifecycle evidence harness."""
import runpy


def main():
    runpy.run_module("aap_libreyolo_plugin.gpu_evidence", run_name="__main__")
