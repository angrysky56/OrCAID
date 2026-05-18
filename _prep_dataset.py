from datasets import Dataset
import pandas as pd

data = [{'repo': 'hw273', 'instance_id': 'minitorch-0', 'repo_branch': 'commit0_combined', 'base_commit': 'HEAD~5'}]
ds = Dataset.from_pandas(pd.DataFrame(data))
ds.save_to_disk('data/commit0/commit0_combined')
print('Dataset saved, columns:', ds.column_names)