import argparse
import sys

from src import database
from src.export import export_section


def main():
    parser = argparse.ArgumentParser(description="Exporta los envíos de una sección a CSV.")
    parser.add_argument("section", choices=("01", "02"))
    parser.add_argument("--db", default=database.DB_PATH)
    args = parser.parse_args()
    export_section(args.section, sys.stdout, args.db)


if __name__ == "__main__":
    main()
