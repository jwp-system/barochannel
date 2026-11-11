"""엑셀 파일 읽기와 쓰기. 업로드 파일은 믿지 않으므로 크기부터 제한하고 값만 읽는다."""

import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter, quote_sheetname
from openpyxl.worksheet.datavalidation import DataValidation

MAX_FILE_BYTES = 2 * 1024 * 1024
# 압축을 풀면 크게 부풀어 오르는 파일을 열기 전에 막는다.
MAX_UNPACKED_BYTES = 30 * 1024 * 1024
MAX_ROWS = 5000
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class SheetError(ValueError):
    """파일 전체를 읽을 수 없을 때. 행 단위 문제는 결과에 따로 모은다."""


@dataclass(frozen=True)
class SheetSpec:
    columns: tuple[str, ...]
    required: tuple[str, ...]


@dataclass(frozen=True)
class Row:
    number: int
    values: dict[str, str]

    def get(self, column: str) -> str:
        return self.values.get(column, "")


def cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        # 숫자만 적은 비밀번호나 이름이 12345678.0으로 바뀌지 않게 한다.
        return str(int(value))
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value).strip()


def _check_archive(data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise SheetError(f"파일은 {MAX_FILE_BYTES // 1024 // 1024}MB까지 올릴 수 있습니다.")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            unpacked = sum(info.file_size for info in archive.infolist())
    except zipfile.BadZipFile as error:
        raise SheetError("엑셀(.xlsx) 파일이 아닙니다.") from error
    if unpacked > MAX_UNPACKED_BYTES:
        raise SheetError("파일 내용이 너무 큽니다. 행을 나누어 올려 주세요.")


def _read_sheet(sheet, name: str, spec: SheetSpec) -> list[Row]:
    rows = sheet.iter_rows(values_only=True)
    header = [cell_text(value).rstrip("*").strip() for value in next(rows, None) or ()]
    missing = [column for column in spec.required if column not in header]
    if missing:
        raise SheetError(f"'{name}' 시트 첫 행에 {', '.join(missing)} 열이 없습니다.")
    index = {column: header.index(column) for column in spec.columns if column in header}
    result: list[Row] = []
    for number, values in enumerate(rows, start=2):
        texts = {
            column: cell_text(values[position]) if position < len(values) else ""
            for column, position in index.items()
        }
        if not any(texts.values()):
            continue
        if len(result) >= MAX_ROWS:
            raise SheetError(f"'{name}' 시트는 {MAX_ROWS}행까지 올릴 수 있습니다.")
        result.append(Row(number, texts))
    return result


def read_workbook(data: bytes, specs: dict[str, SheetSpec]) -> dict[str, list[Row] | None]:
    """시트 이름마다 행을 돌려준다. 없는 시트는 None이다. 수식은 계산된 값만 읽는다."""
    _check_archive(data)
    try:
        workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as error:  # openpyxl은 손상된 파일에 여러 종류의 예외를 낸다.
        raise SheetError("엑셀(.xlsx) 파일을 읽을 수 없습니다.") from error
    try:
        return {
            name: _read_sheet(workbook[name], name, spec) if name in workbook.sheetnames else None
            for name, spec in specs.items()
        }
    except SheetError:
        raise
    except Exception as error:
        raise SheetError("엑셀(.xlsx) 파일을 읽을 수 없습니다.") from error
    finally:
        workbook.close()


HEADER_FONT = Font(bold=True)
HEADER_FILL = PatternFill("solid", fgColor="E8EEF7")
CHOICE_SHEET = "선택 목록"


@dataclass(frozen=True)
class Choice:
    """칸에 다는 드롭다운. 값 목록(values)이나 다른 시트의 열(source=(시트, 머리글))에서 고른다.

    strict이면 목록에 없는 값을 엑셀이 막고, 아니면 경고만 하고 직접 적게 둔다.
    엑셀의 드롭다운은 입력 편의일 뿐이다. 붙여넣기로 우회되므로 서버 검사는 그대로 한다.
    """

    values: tuple[str, ...] = ()
    source: tuple[str, str] | None = None
    strict: bool = True


def _column(sheet, header: str) -> str | None:
    headers = [cell.value for cell in sheet[1]]
    return get_column_letter(headers.index(header) + 1) if header in headers else None


def _add_choices(workbook: Workbook, choices: dict[tuple[str, str], Choice]) -> None:
    lists = None
    for (title, header), choice in choices.items():
        sheet = workbook[title]
        letter = _column(sheet, header)
        if letter is None:
            continue
        if choice.source is not None:
            source_title, source_header = choice.source
            source = _column(workbook[source_title], source_header)
            if source is None:
                continue
            # 같은 파일에서 새로 적은 행도 바로 고를 수 있게 열 전체를 가리킨다.
            formula = f"{quote_sheetname(source_title)}!${source}$2:${source}${MAX_ROWS + 1}"
        else:
            if not choice.values:
                continue
            if lists is None:
                lists = workbook.create_sheet(CHOICE_SHEET)
                lists.sheet_state = "hidden"
            position = 1 if lists["A1"].value is None else lists.max_column + 1
            target = get_column_letter(position)
            lists.cell(row=1, column=position, value=f"{title} · {header}")
            for row, value in enumerate(choice.values, start=2):
                lists.cell(row=row, column=position, value=value).data_type = "s"
            last = len(choice.values) + 1
            formula = f"{quote_sheetname(CHOICE_SHEET)}!${target}$2:${target}${last}"
        validation = DataValidation(
            type="list",
            formula1=formula,
            allow_blank=True,
            showErrorMessage=True,
            errorStyle="stop" if choice.strict else "warning",
            errorTitle=header,
            error=(
                "목록에서 골라 주세요."
                if choice.strict
                else "목록에 없는 값입니다. 그대로 쓰려면 '예'를 누르세요."
            ),
        )
        validation.add(f"{letter}2:{letter}{MAX_ROWS + 1}")
        sheet.add_data_validation(validation)


def build_workbook(
    sheets: list[tuple[str, list[str], list[list[str]]]],
    guide: list[str],
    choices: dict[tuple[str, str], Choice] | None = None,
) -> bytes:
    """choices는 (시트, 머리글)마다 다는 드롭다운이다."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, headers, rows in sheets:
        sheet = workbook.create_sheet(title)
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
        for cell in sheet[1]:
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                # '='로 시작하는 이름이 엑셀에서 수식으로 실행되지 않도록 글자로 고정한다.
                if isinstance(cell.value, str):
                    cell.data_type = "s"
        for position, header in enumerate(headers):
            letter = sheet.cell(row=1, column=position + 1).column_letter
            longest = max([len(header), *(len(str(row[position])) for row in rows)])
            sheet.column_dimensions[letter].width = min(max(longest * 1.6 + 2, 10), 60)
        sheet.freeze_panes = "A2"
    notes = workbook.create_sheet("안내")
    for line in guide:
        notes.append([line])
        notes.cell(row=notes.max_row, column=1).data_type = "s"
    notes.column_dimensions["A"].width = 100
    for cell in notes["A"]:
        cell.alignment = Alignment(wrap_text=True)
    _add_choices(workbook, choices or {})
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
