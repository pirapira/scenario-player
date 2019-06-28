import argparse
import csv
import fileinput
import json
import math
import os
import random
import re
import sys
from datetime import datetime, timedelta
from collections import namedtuple

import numpy as np
import plotly.figure_factory as ff
import plotly.offline as py
from jinja2 import Environment, FileSystemLoader

DEFAULT_GANTT_FILENAME = "gantt-overview.html"
DEFAULT_CSV_FILENAME = "durations.csv"
DEFAULT_SUMMARY_FILENAME = "summary.json"


def has_more_specific_task_bracket(task_bracket, task_indices):
    for other in task_indices:
        if other == task_bracket:
            continue
        if other[0] in range(task_bracket[0], task_bracket[1]):
            # theoretically other[1] == end should also be checked for inclusiveness
            # however we expect to have a tree here where this should not happen
            return True
    return False


def append_subtask(main_task_name, table_rows, csv_rows, subtasks):
    ids = {t[2]['id'] for t in subtasks}
    for id in ids:
        filtered_tasks = []
        for idx, subtask in enumerate(subtasks):
            if "id" not in subtask[2]:
                continue
            if id == subtask[2]["id"]:
                filtered_tasks.append(subtask)

        joined_content = "<br>------------------------------------<br>".join(
            map(
                lambda t: json.dumps(t, sort_keys=True, indent=4).replace("\n", "<br>"),
                filtered_tasks,
            )
        )
        start = filtered_tasks[0][0]
        finish = filtered_tasks[-1][0]
        table_rows.append(
            {
                "id": id,
                "name": f"Subtasks(#{id})",
                "duration": calculate_duration(start, finish),
                "description": joined_content,
            }
        )
        csv_rows.append(
            [main_task_name, f"Subtasks(#{id})", calculate_duration(start, finish), ""]
        )


def calculate_duration(start, finish):
    start_datetime = datetime.strptime(start, "%Y-%m-%d %H:%M:%S.%f")
    finish_datetime = datetime.strptime(finish, "%Y-%m-%d %H:%M:%S.%f")
    return finish_datetime - start_datetime


def create_task_brackets(stripped_content):
    task_indices = []
    for i, start_content in enumerate(stripped_content):
        start_event = start_content.event
        start_id = start_content.json["id"]
        if start_event.lower() == "starting task":
            for j, following_content in enumerate(stripped_content[i:], start=i):
                following_event = following_content.event
                following_id = following_content.json["id"]
                if "task" not in following_content.json:
                    continue
                ids_identical = start_id == following_id
                event_matches_successful = following_event.lower() == "task successful"
                event_matches_errored = following_event.lower() == "task errored"
                if ids_identical and (event_matches_successful or event_matches_errored):
                    task_indices.append([i, j])
                    break
    return task_indices


def read_raw_content(input_file):
    content = []
    if not input_file:
        content = [line.strip() for line in fileinput.input(input_file)]
    else:
        with open(input_file[0]) as f:
            content = [line.strip() for line in f.readlines()]

    stripped_content = []
    Content = namedtuple('Content', 'timestamp, event, json')
    for row in content:
        x = json.loads(row)
        if "id" in x:
            stripped_content.append(Content(x["timestamp"], x["event"], x))

    # sort by timestamp
    stripped_content.sort(key=lambda e: e[0])

    return stripped_content


def draw_gantt(output_directory, filled_rows, summary):
    fig = ff.create_gantt(
        filled_rows['gantt_rows'],
        title="Raiden Analysis",
        show_colorbar=False,
        bar_width=0.5,
        showgrid_x=True,
        showgrid_y=True,
        height=928,
        width=1680,
    )

    fig["layout"].update(
        yaxis={
            # 'showticklabels':False
            "automargin": True
        },
        hoverlabel={"align": "left"},
    )

    div = py.offline.plot(fig, output_type="div")

    j2_env = Environment(
        loader=FileSystemLoader(os.path.dirname(os.path.abspath(__file__))), trim_blocks=True
    )
    output_content = j2_env.get_template("chart_template.html").render(
        gantt_div=div,
        summary_header=f'Tasks matched "{summary["name"]}" (in {summary["unit"]})',
        count=summary["count"],
        min=summary["min"],
        max=summary["max"],
        mean=summary["mean"],
        median=summary["median"],
        stdev=summary["stdev"],
        task_table=filled_rows['table_rows'],
    )

    with open(f"{output_directory}/{DEFAULT_GANTT_FILENAME}", "w") as text_file:
        text_file.write(output_content)


def write_csv(output_directory, csv_rows):
    with open(f"{output_directory}/{DEFAULT_CSV_FILENAME}", "w", newline="") as csv_file:
        csv_writer = csv.writer(csv_file, delimiter=",", quotechar="|", quoting=csv.QUOTE_MINIMAL)
        csv_writer.writerow(["MainTask", "SubTask", "Duration", "Hops"])
        for r in csv_rows:
            csv_writer.writerow(r)


def generate_summary(filled_rows):
    result = {}
    # TODO: match too greedy
    duration_transfers = list(
        map(
            lambda r: r[2].total_seconds(),
            filter(lambda r: r[0].lower().startswith("transfer"), filled_rows['csv_rows']),
        )
    )
    data = np.array(duration_transfers)
    result["name"] = "Transfer"
    result["unit"] = "Seconds"
    result["min"] = data.min()
    result["max"] = data.max()
    result["mean"] = data.mean()
    result["median"] = np.median(data)
    result["stdev"] = data.std()
    result["count"] = data.size
    return result


def write_summary(output_directory, summary):
    with open(f"{output_directory}/{DEFAULT_SUMMARY_FILENAME}", "w", newline="") as summary_file:
        json.dump(summary, summary_file)


def fill_rows(task_brackets, stripped_content):
    filled_rows = dict()
    gantt_rows = []
    csv_rows = []
    table_rows = []

    for task_bracket in task_brackets:
        task_start_item = stripped_content[task_bracket[0]]
        task_finish_item = stripped_content[task_bracket[1]]
        task_body = task_start_item.json["task"].split(":", 1)
        task_id = task_start_item.json["id"]
        task_name = f'{task_body[0].replace("<", "").strip()}(#{task_id})'
        task_desc = task_body[1].replace(">", "").strip()
        task_body_json = json.loads(task_desc.replace("'", '"'))
        duration = calculate_duration(task_start_item.timestamp, task_finish_item.timestamp)

        # add main task to rows
        task_full_desc = json.dumps(task_body_json, sort_keys=True, indent=4).replace("\n", "<br>")
        gantt_rows.append(
            {
                "Task": task_name,
                "Start": task_start_item.timestamp,
                "Finish": task_finish_item.timestamp,
                "Description": task_full_desc,
            }
        )
        hops = ""
        if task_name.startswith("Transfer"):
            transfer_from = task_body_json["from"]
            transfer_to = task_body_json["to"]
            hops = str(abs(transfer_from - transfer_to))
        table_rows.append(
            {
                "id": task_id,
                "name": task_name,
                "duration": duration,
                "description": task_full_desc,
                "hops": hops,
            }
        )
        csv_rows.append(
            [task_name, "", duration, hops]
        )

        # Only add subtasks for leafs of the tree
        main_task_debug_string = f"{str(task_bracket)} {task_name}: {task_desc}"
        if not has_more_specific_task_bracket(task_bracket, task_brackets):
            subtasks = list(filter(lambda t: t.json["id"] == task_id, stripped_content))
            print(f"{main_task_debug_string} - subtasks = {len(subtasks)}")
            print("----------------------------------------------------------------")
            append_subtask(task_name, table_rows, csv_rows, subtasks)
        else:
            print(main_task_debug_string)
            print("----------------------------------------------------------------")
    
    filled_rows['gantt_rows'] = gantt_rows
    filled_rows['csv_rows'] = csv_rows
    filled_rows['table_rows'] = table_rows
    return filled_rows


def parse_args():
    parser = argparse.ArgumentParser(description="Raiden Scenario-Player Analysis")
    parser.add_argument(
        "input_file",
        nargs="*",
        help="File name of scenario-player log file as main input, if empty, stdin is used",
    )
    parser.add_argument(
        "--output-directory",
        type=str,
        dest="output_directory",
        default="raiden-scenario-player-analysis",
        help="Output directory name",
    )

    args = parser.parse_args()

    return args


def main():
    args = parse_args()

    stripped_content = read_raw_content(args.input_file)

    task_brackets = create_task_brackets(stripped_content)

    filled_rows = fill_rows(task_brackets, stripped_content)
    summary = generate_summary(filled_rows)

    if not os.path.exists(args.output_directory):
        os.makedirs(args.output_directory)

    draw_gantt(args.output_directory, filled_rows, summary)
    write_csv(args.output_directory, filled_rows)
    write_summary(args.output_directory, summary)


if __name__ == "__main__":
    main()
